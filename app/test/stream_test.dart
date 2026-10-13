import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:ag_ui/ag_ui.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:aos_display/bridge_client.dart';
import 'package:aos_display/display_controller.dart';

import 'contracts_test.dart' show fixture, loadContracts;

Future<void> waitFor(bool Function() condition) async {
  final deadline = DateTime.now().add(const Duration(seconds: 5));
  while (!condition()) {
    if (DateTime.now().isAfter(deadline)) {
      throw TimeoutException('Condition never became true');
    }
    await Future<void>.delayed(const Duration(milliseconds: 10));
  }
}

Future<void> frame(HttpResponse response, Object? value) async {
  if (response.headers.contentType?.mimeType != 'text/event-stream') {
    response.headers.contentType = ContentType('text', 'event-stream');
    response.bufferOutput = false;
  }
  response.write('data: ${jsonEncode(value)}\n\n');
  await response.flush();
}

Map<String, Object?> snapshot([int revision = 0]) {
  final state = fixture('examples/dashboard.json') as Map;
  state['revision'] = revision;
  return {'type': 'STATE_SNAPSHOT', 'snapshot': state};
}

void main() {
  late HttpServer server;
  late Directory temporary;
  late File token;
  late DisplayConfig config;
  final controllers = <DisplayController>[];
  final clients = <BridgeClient>[];

  setUp(() async {
    server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    temporary = await Directory('.dart_tool').createTemp('stream-test-');
    token = File('${temporary.path}/display.token');
    await token.writeAsString('test-token\n');
    config = DisplayConfig(
      bridgeUrl: Uri.parse('http://127.0.0.1:${server.port}'),
      dashboardPath: '${temporary.path}/dashboard.json',
      tokenPath: token.path,
    );
  });
  tearDown(() async {
    for (final controller in controllers) {
      controller.dispose();
    }
    controllers.clear();
    for (final client in clients) {
      client.close();
    }
    clients.clear();
    await server.close(force: true);
    await temporary.delete(recursive: true);
  });

  DisplayController controller({Duration idle = const Duration(seconds: 2)}) {
    final contracts = loadContracts();
    final result = DisplayController(
      contracts: contracts,
      client: BridgeClient(config, idleTimeout: idle),
      dashboard: contracts.dashboard(fixture('examples/dashboard.json')),
      retryBase: const Duration(milliseconds: 20),
    );
    controllers.add(result);
    return result;
  }

  test(
    'public SseClient.connect integrates authenticated snapshot-first GET and typed decoding',
    () async {
      server.listen((request) async {
        expect(request.uri.path, '/v1/stream');
        expect(request.headers.value('authorization'), 'Bearer test-token');
        await frame(request.response, snapshot());
        await frame(request.response, {
          'type': 'CUSTOM',
          'name': 'aos.heartbeat',
          'value': {'revision': 0},
        });
        // remain connected until explicit close; this is a runless GET feed.
      });
      final sdk = SseClient(idleTimeout: const Duration(seconds: 1));
      try {
        final events = <BaseEvent>[];
        final received = Completer<void>();
        sdk
            .connect(
              config.bridgeUrl.resolve('/v1/stream'),
              headers: {'Authorization': 'Bearer ${await config.readToken()}'},
              requestTimeout: const Duration(seconds: 2),
            )
            .listen(
              (message) {
                events.add(EventDecoder().decode(message.data!));
                if (events.length == 2) received.complete();
              },
              onError: (Object error) {
                if (!received.isCompleted) received.completeError(error);
              },
            );
        await received.future.timeout(const Duration(seconds: 3));
        expect(events[0], isA<StateSnapshotEvent>());
        expect(events[1], isA<CustomEvent>());
      } finally {
        unawaited(sdk.close().catchError((Object _) {}));
      }
    },
  );

  test(
    'upstream initial-failure path emits error but never retries itself',
    () async {
      var attempts = 0;
      server.listen((request) async {
        attempts++;
        request.response.statusCode = 503;
        await request.response.close();
      });
      final sdk = SseClient(idleTimeout: const Duration(milliseconds: 50));
      final failed = Completer<void>();
      final subscription = sdk
          .connect(config.bridgeUrl.resolve('/v1/stream'))
          .listen(
            (_) {},
            onError: (Object _) {
              if (!failed.isCompleted) failed.complete();
            },
          );
      try {
        await failed.future.timeout(const Duration(seconds: 2));
        await Future<void>.delayed(const Duration(milliseconds: 200));
        expect(attempts, 1);
      } finally {
        unawaited(sdk.close().catchError((Object _) {}));
        unawaited(subscription.cancel().catchError((Object _) {}));
      }
    },
  );

  test(
    'upstream idle timeout loses reconnect URL, requiring transport supervisor',
    () async {
      var attempts = 0;
      server.listen((request) async {
        attempts++;
        await frame(request.response, snapshot());
      });
      final sdk = SseClient(idleTimeout: const Duration(milliseconds: 50));
      final received = Completer<void>();
      final subscription = sdk
          .connect(config.bridgeUrl.resolve('/v1/stream'))
          .listen((_) {
            if (!received.isCompleted) received.complete();
          });
      try {
        await received.future.timeout(const Duration(seconds: 2));
        await Future<void>.delayed(const Duration(milliseconds: 250));
        expect(attempts, 1);
      } finally {
        unawaited(sdk.close().catchError((Object _) {}));
        unawaited(subscription.cancel().catchError((Object _) {}));
      }
    },
  );

  test(
    'supervisor recovers initial failure, reads rotated token and rehydrates',
    () async {
      var attempts = 0;
      final auth = <String?>[];
      server.listen((request) async {
        attempts++;
        auth.add(request.headers.value('authorization'));
        if (attempts == 1) {
          await token.writeAsString('rotated-token');
          request.response.statusCode = 503;
          await request.response.close();
        } else {
          await frame(request.response, snapshot(7));
        }
      });
      final display = controller();
      display.start();
      await waitFor(() => display.dashboard.revision == 7);
      expect(auth.take(2), ['Bearer test-token', 'Bearer rotated-token']);
      expect(display.connection, 'Live');
      expect(display.streamDiagnostic, isNull);
    },
  );

  test(
    'supervisor idle timeout reconnects for a snapshot rather than replaying',
    () async {
      var attempts = 0;
      server.listen((request) async {
        attempts++;
        await frame(request.response, snapshot(attempts));
      });
      final display = controller(idle: const Duration(milliseconds: 80));
      display.start();
      await waitFor(() => attempts >= 2 && display.dashboard.revision == 2);
      expect(display.connection, 'Live');
    },
  );

  test(
    'bad delta retains last good state, reconnects and rehydrates atomically',
    () async {
      var attempts = 0;
      final seen = <int>[];
      server.listen((request) async {
        attempts++;
        await frame(request.response, snapshot(attempts == 1 ? 0 : 2));
        if (attempts == 1) {
          await frame(request.response, {
            'type': 'STATE_DELTA',
            'delta': [
              {'op': 'test', 'path': '/revision', 'value': 0},
              {'op': 'replace', 'path': '/revision', 'value': 1},
              {
                'op': 'replace',
                'path': '/sections/2/items/0/done',
                'value': 'invalid',
              },
            ],
          });
        }
      });
      final display = controller();
      display.addListener(() => seen.add(display.dashboard.revision));
      display.start();
      await waitFor(() => display.dashboard.revision == 2);
      expect(seen, isNot(contains(1)));
      expect(attempts, greaterThanOrEqualTo(2));
    },
  );

  test(
    'snapshot-first, heartbeat revision and corruption checks preserve state',
    () {
      final display = controller();
      expect(
        () => display.acceptFrame(jsonEncode(fixture('examples/delta.json'))),
        throwsFormatException,
      );
      display.acceptFrame(jsonEncode(snapshot()));
      final original = display.dashboard;
      display.acceptFrame(
        jsonEncode({
          'type': 'CUSTOM',
          'name': 'aos.heartbeat',
          'value': {'revision': 0},
        }),
      );
      expect(identical(display.dashboard, original), isTrue);
      expect(() => display.acceptFrame('bad JSON'), throwsFormatException);
      expect(
        () => display.acceptFrame(
          jsonEncode({
            'type': 'CUSTOM',
            'name': 'aos.heartbeat',
            'value': {'revision': 1},
          }),
        ),
        throwsFormatException,
      );
      expect(identical(display.dashboard, original), isTrue);
    },
  );

  test(
    'native transport fails closed without token and refuses non-loopback URL',
    () async {
      var attempts = 0;
      server.listen((request) async {
        attempts++;
        request.response.close();
      });
      await token.delete();
      final client = BridgeClient(config);
      clients.add(client);
      await expectLater(
        client.stream().first,
        throwsA(isA<FileSystemException>()),
      );
      expect(attempts, 0);
      expect(
        () => DisplayConfig(
          bridgeUrl: Uri.parse('http://example.com:8080'),
          dashboardPath: 'unused',
          tokenPath: 'unused',
        ),
        throwsFormatException,
      );
    },
  );

  test(
    'native transport parses chunked multiline SSE and rejects oversized unterminated lines',
    () async {
      var attempts = 0;
      server.listen((request) async {
        attempts++;
        final response = request.response;
        response.headers.contentType = ContentType('text', 'event-stream');
        response.bufferOutput = false;
        if (attempts == 1) {
          response.write('data: {"type":"CUSTOM",\n');
          await response.flush();
          response.write(
            'data: "name":"aos.heartbeat","value":{"revision":0}}\n\n',
          );
          await response.flush();
        } else {
          response.write('data: ${'x' * (128 * 1024 + 2048)}');
          await response.close();
        }
      });
      final client = BridgeClient(config);
      clients.add(client);
      final data = await client.stream().first;
      expect(EventDecoder().decode(data), isA<CustomEvent>());
      await expectLater(client.stream().first, throwsFormatException);
    },
  );
}
