import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:aos_display/bridge_client.dart';
import 'package:aos_display/contracts.dart';
import 'package:aos_display/display_controller.dart';
import 'package:flutter_test/flutter_test.dart';

import 'contracts_test.dart' show fixture, loadContracts;

Future<void> eventually(bool Function() condition) async {
  final deadline = DateTime.now().add(const Duration(seconds: 10));
  while (!condition()) {
    if (DateTime.now().isAfter(deadline)) {
      throw TimeoutException('Real bridge condition not reached');
    }
    await Future<void>.delayed(const Duration(milliseconds: 20));
  }
}

void main() {
  test(
    'real Python bridge persists updates, streams them, and queues TV intents',
    () async {
      final root = Directory('..').absolute.path;
      final python =
          Platform.environment['AOS_TEST_PYTHON'] ?? '$root/.venv/bin/python';
      expect(
        File(python).existsSync(),
        isTrue,
        reason: 'Run uv sync --frozen in the repository root first',
      );
      final temporary = await Directory(
        '.dart_tool',
      ).createTemp('real-bridge-');
      final token = File('${temporary.path}/display.token');
      await token.writeAsString('display-test-credential');
      final allocated = await ServerSocket.bind(
        InternetAddress.loopbackIPv4,
        0,
      );
      final port = allocated.port;
      await allocated.close();
      final process = await Process.start(
        python,
        ['-m', 'aos_bridge', '--port', '$port'],
        workingDirectory: root,
        includeParentEnvironment: false,
        environment: {
          ...Map<String, String>.from(Platform.environment)
            ..removeWhere((key, _) => key.startsWith('AOS_')),
          'AOS_STATE_DIR': temporary.absolute.path,
          'AOS_BRIDGE_TOKEN': 'bridge-test-credential',
          'AOS_DISPLAY_TOKEN': 'display-test-credential',
          'AOS_MUSE_TOKEN': 'muse-test-credential',
        },
      );
      final output = <String>[];
      final stdout = process.stdout.transform(utf8.decoder).listen(output.add);
      final stderr = process.stderr.transform(utf8.decoder).listen(output.add);
      final host = Uri.parse('http://127.0.0.1:$port');
      final http = HttpClient()..findProxy = (_) => 'DIRECT';
      DisplayController? controller;
      BridgeClient? display;
      try {
        Future<Map<String, dynamic>> call(
          String method,
          String path, [
          Object? body,
        ]) async {
          final request = await http.openUrl(method, host.resolve(path));
          request.headers.set('authorization', 'Bearer bridge-test-credential');
          if (body != null) {
            request.headers.contentType = ContentType.json;
            request.write(jsonEncode(body));
          }
          final response = await request.close();
          final content = await response.transform(utf8.decoder).join();
          expect(response.statusCode, 200, reason: content);
          return jsonDecode(content) as Map<String, dynamic>;
        }

        final deadline = DateTime.now().add(const Duration(seconds: 10));
        while (true) {
          try {
            await call('GET', '/v1/dashboard');
            break;
          } on SocketException {
            if (DateTime.now().isAfter(deadline)) rethrow;
            await Future<void>.delayed(const Duration(milliseconds: 50));
          }
        }
        display = BridgeClient(
          DisplayConfig(
            bridgeUrl: host,
            dashboardPath: '${temporary.path}/dashboard.json',
            tokenPath: token.path,
          ),
        );
        controller = DisplayController(
          contracts: loadContracts(),
          client: display,
          dashboard: loadContracts().dashboard(
            fixture('examples/dashboard.json'),
          ),
          retryBase: const Duration(milliseconds: 20),
        );
        controller.start();
        final current = controller;
        await eventually(() => current.connection == 'Live');
        final state = await call('GET', '/v1/dashboard');
        final payload = Map<String, dynamic>.from(state)
          ..remove('revision')
          ..remove('updated_at');
        (payload['sections'] as List).firstWhere(
          (section) => section['type'] == 'briefing',
        )['headline'] = 'Published through the real bridge';
        await call('POST', '/v1/dashboard', {
          'payload': payload,
          'expected_revision': 0,
        });
        await eventually(() => current.dashboard.revision == 1);
        final persisted =
            jsonDecode(
                  await File('${temporary.path}/dashboard.json').readAsString(),
                )
                as Map;
        expect(persisted['revision'], 1);
        expect(
          current.dashboard.sections.firstWhere(
            (section) => section['type'] == 'briefing',
          )['headline'],
          'Published through the real bridge',
        );
        await current.toggle('todos', 't1');
        final queue = await call('GET', '/v1/events');
        final event = (queue['events'] as List).single as Map;
        expect(event['kind'], 'todo_toggle');
        expect(event['payload']['observed_revision'], 1);
        expect(event['payload']['target_id'], 't1');
        expect(current.pending, isNotEmpty);
        final response = await http.postUrl(host.resolve('/v1/ingest'));
        response.headers.set('authorization', 'Bearer bridge-test-credential');
        response.headers.set('if-match', '"1"');
        response.headers.contentType = ContentType.json;
        response.write(
          jsonEncode({
            'type': 'STATE_DELTA',
            'delta': [
              {
                'op': 'replace',
                'path': '/sections/2/items/0/done',
                'value': true,
              },
            ],
          }),
        );
        final patch = await response.close();
        expect(patch.statusCode, 200);
        await patch.drain<void>();
        await eventually(
          () => current.dashboard.revision == 2 && current.pending.isEmpty,
        );
        await call('POST', '/v1/events/ack', {
          'ids': [event['id']],
        });
        expect((await call('GET', '/v1/events'))['events'], isEmpty);
        final large =
            jsonDecode(jsonEncode(current.dashboard.data))
                as Map<String, dynamic>;
        final future = <String, dynamic>{
          'id': 'future',
          'type': 'future',
          'blob': '',
        };
        (large['sections'] as List).add(future);
        future['blob'] =
            'x' *
            (Contracts.maxBytes - 16 - utf8.encode(jsonEncode(large)).length);
        final replacement = Map<String, dynamic>.from(large)
          ..remove('revision')
          ..remove('updated_at');
        final committed = await call('POST', '/v1/dashboard', {
          'payload': replacement,
          'expected_revision': 2,
        });
        await eventually(() => current.dashboard.revision == 3);
        expect(current.dashboard.data, committed);
        final reconnected = BridgeClient(display.config);
        try {
          final data = await reconnected.stream().first.timeout(
            const Duration(seconds: 5),
          );
          expect(utf8.encode(data).length, greaterThan(Contracts.maxBytes));
          expect(
            utf8.encode(data).length,
            lessThanOrEqualTo(Contracts.maxEventBytes),
          );
          final snapshot = jsonDecode(data) as Map<String, dynamic>;
          loadContracts().event(snapshot);
          expect(
            loadContracts().dashboard(snapshot['snapshot']).data,
            committed,
          );
        } finally {
          reconnected.close();
        }
      } finally {
        controller?.dispose();
        display?.close();
        http.close(force: true);
        process.kill(ProcessSignal.sigterm);
        await process.exitCode.timeout(const Duration(seconds: 10));
        await stdout.cancel();
        await stderr.cancel();
        await temporary.delete(recursive: true);
      }
    },
    timeout: const Timeout(Duration(seconds: 40)),
  );
}
