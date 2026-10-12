import 'dart:async';
import 'dart:convert';
import 'dart:io';

import 'package:flutter_test/flutter_test.dart';

import 'package:aos_display/bridge_client.dart';
import 'package:aos_display/display_controller.dart';

import 'contracts_test.dart' show fixture, loadContracts;

void main() {
  late HttpServer server;
  late Directory temporary;
  late DisplayController display;

  setUp(() async {
    server = await HttpServer.bind(InternetAddress.loopbackIPv4, 0);
    temporary = await Directory('.dart_tool').createTemp('input-test-');
    final token = File('${temporary.path}/display.token');
    await token.writeAsString('test-token');
    final contracts = loadContracts();
    display = DisplayController(
      contracts: contracts,
      client: BridgeClient(
        DisplayConfig(
          bridgeUrl: Uri.parse('http://127.0.0.1:${server.port}'),
          dashboardPath: '${temporary.path}/dashboard.json',
          tokenPath: token.path,
        ),
      ),
      dashboard: contracts.dashboard(fixture('examples/dashboard.json')),
    );
    display.acceptFrame(jsonEncode(fixture('examples/snapshot.json')));
  });
  tearDown(() async {
    display.dispose();
    await server.close(force: true);
    await temporary.delete(recursive: true);
  });

  test(
    'todo submits a durable intent, stays pending, and only authoritative state checks it',
    () async {
      final received = <Map>[];
      server.listen((request) async {
        expect(request.uri.path, '/v1/input');
        expect(request.headers.value('authorization'), 'Bearer test-token');
        final payload =
            jsonDecode(await utf8.decoder.bind(request).join()) as Map;
        display.contracts.input(payload);
        received.add(payload);
        request.response.headers.contentType = ContentType.json;
        request.response.write('{"id":1}');
        await request.response.close();
      });
      await display.toggle('todos', 't1');
      expect(received.single['observed_revision'], 0);
      expect(received.single['section_id'], 'todos');
      expect(received.single['target_id'], 't1');
      expect(received.single['done'], true);
      expect((received.single['client_event_id'] as String).length, 32);
      expect(display.pending['todos/t1']?.accepted, isTrue);
      expect(
        (display.dashboard.section('todos')!['items'] as List)[0]['done'],
        false,
      );
      await display.toggle('todos', 't1');
      expect(received.length, 1);
      display.acceptFrame(jsonEncode(fixture('examples/delta.json')));
      expect(display.pending, isEmpty);
      expect(
        (display.dashboard.section('todos')!['items'] as List)[0]['done'],
        true,
      );
      expect(
        received.length,
        1,
        reason: 'rendering state never emits another input',
      );
    },
  );

  test(
    'failed/ambiguous receipt retries the same client ID and exact revision payload',
    () async {
      final received = <Object?>[];
      server.listen((request) async {
        received.add(jsonDecode(await utf8.decoder.bind(request).join()));
        if (received.length == 1) {
          request.response.statusCode = 503;
        } else {
          request.response.headers.contentType = ContentType.json;
          request.response.write('{"id":2}');
        }
        await request.response.close();
      });
      await display.toggle('todos', 't2');
      expect(display.pending['todos/t2']?.failed, isTrue);
      expect(display.inputDiagnostic, contains('not confirmed'));
      await display.toggle('todos', 't2');
      expect(received[1], received[0]);
      expect(display.pending['todos/t2']?.accepted, isTrue);
      expect(display.dashboard.revision, 0);
    },
  );

  test(
    'unrelated revision and heartbeat never clear pending intent or freshness',
    () async {
      server.listen((request) async {
        request.response.headers.contentType = ContentType.json;
        request.response.write('{"id":1}');
        await request.response.close();
      });
      await display.toggle('todos', 't1');
      display.acceptFrame(
        jsonEncode({
          'type': 'STATE_DELTA',
          'delta': [
            {'op': 'replace', 'path': '/revision', 'value': 1},
            {
              'op': 'replace',
              'path': '/sections/5/note',
              'value': 'Other content changed',
            },
          ],
        }),
      );
      final time = display.dashboard.updatedAt;
      display.acceptFrame(
        jsonEncode({
          'type': 'CUSTOM',
          'name': 'aos.heartbeat',
          'value': {'revision': 1},
        }),
      );
      expect(display.pending['todos/t1'], isNotNull);
      expect(display.dashboard.updatedAt, time);
    },
  );

  test(
    'overlay dismissal is durable input and does not update content age',
    () async {
      Map? received;
      server.listen((request) async {
        received = jsonDecode(await utf8.decoder.bind(request).join()) as Map;
        display.contracts.input(received);
        request.response.headers.contentType = ContentType.json;
        request.response.write('{"id":3}');
        await request.response.close();
      });
      final original = display.dashboard;
      display.acceptFrame(jsonEncode(fixture('examples/overlay.json')));
      expect(display.overlay, isNotNull);
      await display.dismissOverlay();
      expect(display.overlay, isNull);
      expect(received!['kind'], 'overlay_dismissed');
      expect(identical(display.dashboard, original), isTrue);
    },
  );

  test(
    'typed remote and device custom events never simulate hardware success',
    () {
      final original = display.dashboard;
      display.acceptFrame(
        jsonEncode({
          'type': 'CUSTOM',
          'name': 'aos.device',
          'value': {'action': 'power_off'},
        }),
      );
      display.acceptFrame(
        jsonEncode({
          'type': 'CUSTOM',
          'name': 'aos.remote_key',
          'value': {'key': 'ok'},
        }),
      );
      expect(identical(display.dashboard, original), isTrue);
      expect(display.pending, isEmpty);
    },
  );

  test('delayed old dismissal never clears a newer notification', () async {
    final received = Completer<Map>();
    final release = Completer<void>();
    server.listen((request) async {
      received.complete(
        jsonDecode(await utf8.decoder.bind(request).join()) as Map,
      );
      await release.future;
      request.response.headers.contentType = ContentType.json;
      request.response.write('{"id":3}');
      await request.response.close();
    });
    display.acceptFrame(jsonEncode(fixture('examples/overlay.json')));
    final pending = display.dismissOverlay();
    final intent = await received.future;
    expect(intent['overlay_id'], 'sample-overlay');
    final next = fixture('examples/overlay.json') as Map;
    next['value']['overlay_id'] = 'new-overlay';
    next['value']['message'] = 'New notification';
    display.acceptFrame(jsonEncode(next));
    display.acceptFrame(
      jsonEncode({
        'type': 'CUSTOM',
        'name': 'aos.overlay_dismissed',
        'value': {'overlay_id': intent['overlay_id']},
      }),
    );
    release.complete();
    await pending;
    expect(display.overlay, 'New notification');
    display.acceptFrame(
      jsonEncode({
        'type': 'CUSTOM',
        'name': 'aos.overlay_dismissed',
        'value': {'overlay_id': 'new-overlay'},
      }),
    );
    expect(display.overlay, isNull);
  });

  test(
    'authoritative overlay dismissal and duration expiry clear overlays',
    () async {
      display.acceptFrame(
        jsonEncode({
          'type': 'CUSTOM',
          'name': 'aos.overlay',
          'value': {
            'message': 'Short notification',
            'duration_s': 1,
            'overlay_id': 'short-overlay',
          },
        }),
      );
      await Future<void>.delayed(const Duration(milliseconds: 1100));
      expect(display.overlay, isNull);
      display.acceptFrame(jsonEncode(fixture('examples/overlay.json')));
      display.acceptFrame(
        jsonEncode({
          'type': 'CUSTOM',
          'name': 'aos.overlay_dismissed',
          'value': {'overlay_id': 'sample-overlay'},
        }),
      );
      expect(display.overlay, isNull);
    },
  );
}
