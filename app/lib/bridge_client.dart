import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:ag_ui/ag_ui.dart';

import 'contracts.dart';

class DisplayConfig {
  DisplayConfig({
    required this.bridgeUrl,
    required this.dashboardPath,
    required this.tokenPath,
  }) {
    if (bridgeUrl.scheme != 'http' ||
        !{'127.0.0.1', 'localhost', '::1'}.contains(bridgeUrl.host) ||
        bridgeUrl.userInfo.isNotEmpty ||
        bridgeUrl.hasQuery ||
        bridgeUrl.hasFragment ||
        !{'', '/'}.contains(bridgeUrl.path)) {
      throw const FormatException('Bridge URL must be a loopback HTTP origin');
    }
  }

  factory DisplayConfig.environment() {
    final env = Platform.environment;
    return DisplayConfig(
      bridgeUrl: Uri.parse(env['AOS_BRIDGE_URL'] ?? 'http://127.0.0.1:8080'),
      dashboardPath: env['AOS_DASHBOARD_PATH'] ?? '/var/lib/aos/dashboard.json',
      tokenPath: env['AOS_DISPLAY_TOKEN_FILE'] ?? '/etc/aos/display.token',
    );
  }

  final Uri bridgeUrl;
  final String dashboardPath;
  final String tokenPath;

  Future<String> readToken() async {
    final file = File(tokenPath);
    if (await file.length() > 4096) {
      throw const FormatException('Display token file exceeds bounds');
    }
    final token = (await file.readAsString()).trim();
    if (token.isEmpty || token.contains(RegExp(r'\s'))) {
      throw const FormatException('Display token file is empty or malformed');
    }
    return token;
  }
}

/// Small native HTTP supervisor fallback for ag_ui 0.3.0 connection defects.
/// Reconnect ownership is in DisplayController, so every attempt reads the token
/// again and requires a new snapshot. Parsing and event decoding remain SDK-owned.
class BridgeClient {
  BridgeClient(
    this.config, {
    this.idleTimeout = const Duration(seconds: 40),
    this.requestTimeout = const Duration(seconds: 8),
  });

  final DisplayConfig config;
  final Duration idleTimeout;
  final Duration requestTimeout;
  HttpClient? _streamClient;
  final Set<HttpClient> _requests = {};
  bool _closed = false;

  HttpClient _client() => HttpClient()
    ..connectionTimeout = requestTimeout
    ..findProxy = (_) => 'DIRECT';

  Future<HttpClientResponse> _open(
    HttpClient client,
    String method,
    String path, {
    Object? payload,
  }) async {
    final token = await config.readToken();
    if (_closed) throw StateError('Display client closed');
    final request = await client
        .openUrl(method, config.bridgeUrl.resolve(path))
        .timeout(requestTimeout);
    request.followRedirects = false;
    request.headers.set(HttpHeaders.authorizationHeader, 'Bearer $token');
    request.headers.set(
      HttpHeaders.acceptHeader,
      path == '/v1/stream' ? 'text/event-stream' : 'application/json',
    );
    if (payload != null) {
      request.headers.contentType = ContentType.json;
      request.write(jsonEncode(payload));
    }
    return request.close().timeout(requestTimeout);
  }

  Stream<String> stream() async* {
    final client = _client();
    final parser = SseClient(maxDataCodeUnits: Contracts.maxEventBytes);
    _streamClient = client;
    try {
      final response = await _open(client, 'GET', '/v1/stream');
      if (response.statusCode != 200 ||
          response.headers.contentType?.mimeType != 'text/event-stream') {
        throw HttpException('Stream rejected (${response.statusCode})');
      }
      // guard the SDK's line splitter before it can accumulate an unlimited line.
      var lineBytes = 0;
      final source = response
          .handleError((Object error) {
            // Intentional close must not escape an SDK async-generator cancellation.
            if (!_closed && identical(_streamClient, client)) throw error;
          })
          .timeout(idleTimeout);
      final bounded = source.map((chunk) {
        for (final byte in chunk) {
          lineBytes = byte == 10 || byte == 13 ? 0 : lineBytes + 1;
          if (lineBytes > Contracts.maxEventBytes + 1024) {
            throw const FormatException('SSE line exceeds bounds');
          }
        }
        return chunk;
      });
      await for (final message in parser.parseStream(bounded)) {
        final data = message.data;
        if (data == null || data.isEmpty) continue;
        if (utf8.encode(data).length > Contracts.maxEventBytes) {
          throw const FormatException('SSE event exceeds 129 KiB');
        }
        yield data;
      }
    } finally {
      parser.close();
      client.close(force: true);
      if (identical(_streamClient, client)) _streamClient = null;
    }
  }

  Future<int> input(Map<String, Object?> intent) async {
    final client = _client();
    _requests.add(client);
    try {
      final response = await _open(
        client,
        'POST',
        '/v1/input',
        payload: intent,
      );
      if (response.statusCode < 200 || response.statusCode >= 300) {
        // no raw response bodies: even a misconfigured server cannot leak tokens.
        throw HttpException('Input rejected (${response.statusCode})');
      }
      final bytes = <int>[];
      await for (final chunk in response.timeout(requestTimeout)) {
        bytes.addAll(chunk);
        if (bytes.length > 4096) {
          throw const FormatException('Input receipt too large');
        }
      }
      final result = jsonDecode(utf8.decode(bytes));
      if (result is! Map || result['id'] is! int || (result['id'] as int) < 1) {
        throw const FormatException('Invalid durable input receipt');
      }
      return result['id'] as int;
    } finally {
      _requests.remove(client);
      client.close(force: true);
    }
  }

  void reconnect() {
    final client = _streamClient;
    _streamClient = null;
    client?.close(force: true);
  }

  void close() {
    _closed = true;
    reconnect();
    for (final client in _requests) {
      client.close(force: true);
    }
  }
}
