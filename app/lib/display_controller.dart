import 'dart:async';
import 'dart:convert';
import 'dart:math';

import 'package:ag_ui/ag_ui.dart';
import 'package:flutter/foundation.dart';

import 'bridge_client.dart';
import 'contracts.dart';

class PendingTodo {
  PendingTodo(this.intent);
  final Map<String, Object?> intent;
  bool sending = false;
  bool accepted = false;
  bool failed = false;
}

class DisplayController extends ChangeNotifier {
  DisplayController({
    required this.contracts,
    required this.client,
    required this.dashboard,
    this.bootDiagnostic,
    this.retryBase = const Duration(seconds: 1),
  });

  final Contracts contracts;
  final BridgeClient client;
  final String? bootDiagnostic;
  final Duration retryBase;
  final EventDecoder _decoder = EventDecoder();
  final Map<String, PendingTodo> pending = {};
  Dashboard dashboard;
  String connection = 'Offline';
  String? streamDiagnostic;
  String? inputDiagnostic;
  String? overlay;
  String? _overlayId;
  Timer? _overlayTimer;
  Timer? _retryTimer;
  bool _alive = true;
  bool _started = false;
  bool _hydrated = false;
  bool _dismissing = false;
  Map<String, Object?>? _dismissIntent;
  int _failures = 0;

  void start() {
    if (_started || !_alive) return;
    _started = true;
    unawaited(_connect());
  }

  Future<void> _connect() async {
    if (!_alive) return;
    _hydrated = false;
    connection = 'Connecting';
    notifyListeners();
    try {
      await for (final data in client.stream()) {
        if (!_alive) break;
        acceptFrame(data);
      }
      if (_alive) throw const FormatException('Stream ended');
    } catch (_) {
      if (!_alive) return;
      // deliberately do not log remote bodies, filesystem errors or credentials.
      streamDiagnostic =
          'Feed interrupted or invalid · retaining last good state';
      connection = 'Reconnecting';
      _hydrated = false;
      notifyListeners();
      final delay = retryBase * (1 << min(_failures++, 5));
      _retryTimer = Timer(delay, () => unawaited(_connect()));
    }
  }

  /// Throws on corrupt/inapplicable frames so the transport rehydrates by snapshot.
  void acceptFrame(String data) {
    final raw = jsonDecode(data);
    if (raw is! Map) throw const FormatException('Event is not an object');
    final type = raw['type'];
    // this is an appliance binding, not a full AG-UI run client. Unknown envelope
    // material is ignored rather than rendered as arbitrary UI.
    if (!{'STATE_SNAPSHOT', 'STATE_DELTA', 'CUSTOM'}.contains(type)) return;
    contracts.event(raw);
    final event = _decoder.decode(data);
    if (!_hydrated && event is! StateSnapshotEvent) {
      throw const FormatException('Feed must begin with a snapshot');
    }
    if (event is StateSnapshotEvent) {
      final next = contracts.dashboard(event.snapshot);
      _publish(next);
      _hydrated = true;
      connection = 'Live';
      streamDiagnostic = null;
      _failures = 0;
    } else if (event is StateDeltaEvent) {
      _publish(contracts.apply(dashboard, event.delta));
    } else if (event is CustomEvent) {
      if (!event.name.startsWith('aos.')) return;
      // unsupported future custom names are safely ignored.
      final supported = (contracts.customSchema.schemaMap!['oneOf'] as List)
          .any(
            (branch) =>
                (branch['properties']['name'] as Map)['const'] == event.name,
          );
      if (!supported) return;
      contracts.custom(raw);
      if (event.name == 'aos.heartbeat') {
        if ((event.value as Map)['revision'] != dashboard.revision) {
          throw const FormatException('Heartbeat revision differs from state');
        }
      } else if (event.name == 'aos.overlay') {
        final value = event.value as Map;
        final id = value['overlay_id'];
        if (id is! String) {
          throw const FormatException('Committed overlay requires an identity');
        }
        _overlayTimer?.cancel();
        _overlayId = id;
        overlay = value['message'] as String;
        _dismissIntent = null;
        _overlayTimer = Timer(
          Duration(milliseconds: ((value['duration_s'] as num) * 1000).round()),
          _clearOverlay,
        );
      } else if (event.name == 'aos.overlay_dismissed') {
        if ((event.value as Map)['overlay_id'] == _overlayId) _clearOverlay();
      }
      // remote_key/device events do not execute device actions in the display.
      // the scoped CEC service injects keyboard events; no duplicate dispatch.
    }
    notifyListeners();
  }

  void _publish(Dashboard next) {
    dashboard = next;
    pending.removeWhere((key, action) {
      if (next.revision <= (action.intent['observed_revision'] as int)) {
        return false;
      }
      final section = next.sections.where(
        (s) => s['id'] == action.intent['section_id'],
      );
      if (section.isEmpty || section.first['type'] != 'todos') return true;
      final items = section.first['items'] as List;
      final target = items.where(
        (item) => (item as Map)['id'] == action.intent['target_id'],
      );
      return target.isEmpty ||
          (target.first as Map)['done'] == action.intent['done'];
    });
  }

  String todoKey(String sectionId, String targetId) => '$sectionId/$targetId';

  Future<void> toggle(String sectionId, String targetId) async {
    if (!_alive) return;
    final key = todoKey(sectionId, targetId);
    var action = pending[key];
    if (action != null && (action.sending || action.accepted)) return;
    if (action == null) {
      final section = dashboard.sections.firstWhere(
        (s) => s['id'] == sectionId,
      );
      final item =
          (section['items'] as List).firstWhere(
                (item) => (item as Map)['id'] == targetId,
              )
              as Map;
      action = PendingTodo({
        ..._intent('todo_toggle'),
        'section_id': sectionId,
        'target_id': targetId,
        'done': !(item['done'] as bool),
      });
      pending[key] = action;
    }
    contracts.input(action.intent);
    action.sending = true;
    action.failed = false;
    inputDiagnostic = null;
    notifyListeners();
    try {
      await client.input(action.intent);
      action.accepted = true;
    } catch (_) {
      action.failed = true;
      if (_alive) {
        inputDiagnostic = 'Input not confirmed · Enter retries the same intent';
      }
    } finally {
      action.sending = false;
      if (_alive) notifyListeners();
    }
  }

  Map<String, Object?> _intent(String kind) {
    final random = Random.secure();
    final id = List.generate(
      16,
      (_) => random.nextInt(256).toRadixString(16).padLeft(2, '0'),
    ).join();
    return {
      'kind': kind,
      'client_event_id': id,
      'observed_revision': dashboard.revision,
    };
  }

  Future<void> dismissOverlay() async {
    if (overlay == null || _dismissing || !_alive) return;
    _dismissing = true;
    _dismissIntent ??= {
      ..._intent('overlay_dismissed'),
      'overlay_id': _overlayId,
    };
    final intent = _dismissIntent!;
    contracts.input(intent);
    try {
      await client.input(intent);
      // a durable receipt permits local dismissal even if the SSE frame was lost.
      if (identical(_dismissIntent, intent)) _clearOverlay();
    } catch (_) {
      if (_alive) {
        inputDiagnostic = 'Dismissal not confirmed · Escape retries';
        notifyListeners();
      }
    } finally {
      _dismissing = false;
    }
  }

  void _clearOverlay() {
    _overlayTimer?.cancel();
    overlay = null;
    _overlayId = null;
    _dismissIntent = null;
    if (_alive) notifyListeners();
  }

  @override
  void dispose() {
    _alive = false;
    _retryTimer?.cancel();
    _overlayTimer?.cancel();
    client.close();
    super.dispose();
  }
}
