import 'dart:convert';
import 'dart:io';

import 'package:ag_ui/ag_ui.dart';
import 'package:flutter_test/flutter_test.dart';
import 'package:json_schema/json_schema.dart';

import 'package:aos_display/contracts.dart';

Object? fixture(String path) =>
    jsonDecode(File('assets/contracts/$path').readAsStringSync());

Contracts loadContracts() => Contracts(
  Map<String, Object?>.from(fixture('dashboard.v1.schema.json') as Map),
  fixture('input.v1.schema.json'),
  fixture('custom-events.schema.json'),
  fixture('ag-ui/schema.json'),
);

void main() {
  final contracts = loadContracts();
  final cases = fixture('fixtures/dashboard-cases.json') as List;
  for (final entry in cases) {
    test('shared dashboard fixture: ${entry['name']}', () {
      if (entry['valid'] == true) {
        expect(contracts.dashboard(entry['state']).revision, 0);
      } else {
        expect(
          () => contracts.dashboard(entry['state']),
          throwsFormatException,
        );
      }
    });
  }

  test(
    'validator actually selects draft 2020-12 and asserts date-time format',
    () {
      expect(
        contracts.dashboardSchema.schemaVersion,
        SchemaVersion.draft2020_12,
      );
      final state = fixture('examples/dashboard.json') as Map;
      state['updated_at'] = '2026-13-01T10:00:00Z';
      expect(() => contracts.dashboard(state), throwsFormatException);
    },
  );

  test('status maxContains and known-type multiplicity are enforced', () {
    for (final type in contracts.knownTypes.where((type) => type != 'todos')) {
      final state = fixture('examples/dashboard.json') as Map;
      final sections = state['sections'] as List;
      sections.add({
        ...sections.firstWhere((s) => s['type'] == type) as Map,
        'id': 'other',
      });
      expect(
        () => contracts.dashboard(state),
        throwsFormatException,
        reason: type,
      );
    }
  });

  test('unique section IDs; todo IDs scoped to each owner', () {
    final state = fixture('examples/dashboard.json') as Map;
    final sections = state['sections'] as List;
    sections[1]['id'] = sections[0]['id'];
    expect(() => contracts.dashboard(state), throwsFormatException);
    sections[1]['id'] = 'agenda';
    sections.add({...sections[2] as Map, 'id': 'second-todos'});
    expect(contracts.dashboard(state).sections.length, 7);
  });

  test('shared events validate and decode with public EventDecoder', () {
    final decoder = EventDecoder();
    for (final name in ['snapshot', 'delta', 'overlay']) {
      final data = fixture('examples/$name.json');
      contracts.event(data);
      expect(decoder.decode(jsonEncode(data)), isA<BaseEvent>());
      if (name == 'overlay') contracts.custom(data);
    }
    contracts.input(fixture('examples/input.json'));
  });

  test('delta fixture applies atomically and exactly once', () {
    final old = contracts.dashboard(fixture('examples/dashboard.json'));
    final delta = (fixture('examples/delta.json') as Map)['delta'] as List;
    final next = contracts.apply(old, delta);
    expect(next.revision, 1);
    expect(old.revision, 0);
    expect(() => contracts.apply(next, delta), throwsFormatException);
    expect(() => old.data['revision'] = 100, throwsUnsupportedError);
  });

  test(
    'invalid final state and failed patch leave previous immutable data intact',
    () {
      final old = contracts.dashboard(fixture('examples/dashboard.json'));
      final before = jsonEncode(old.data);
      final delta = [
        {'op': 'test', 'path': '/revision', 'value': 0},
        {'op': 'replace', 'path': '/revision', 'value': 1},
        {'op': 'replace', 'path': '/sections/2/items/0/done', 'value': 'wrong'},
      ];
      expect(() => contracts.apply(old, delta), throwsFormatException);
      expect(jsonEncode(old.data), before);
      delta[2] = {'op': 'remove', 'path': '/missing'};
      expect(() => contracts.apply(old, delta), throwsA(anything));
      expect(jsonEncode(old.data), before);
    },
  );

  test('oversized state and bounded known lists fail closed', () {
    final state = fixture('examples/dashboard.json') as Map;
    final sections = state['sections'] as List;
    sections[2]['items'] = List.generate(
      41,
      (i) => {'id': 't$i', 'label': 'todo', 'done': false},
    );
    expect(() => contracts.dashboard(state), throwsFormatException);
    sections[2]['items'] = [];
    sections.add({
      'id': 'future',
      'type': 'unknown',
      'data': 'x' * Contracts.maxBytes,
    });
    expect(() => contracts.dashboard(state), throwsFormatException);
  });

  test('installed contract assets are byte-for-byte in sync', () async {
    final result = await Process.run('../.venv/bin/python', [
      'tool/sync_contracts.py',
      '--check',
    ]);
    expect(result.exitCode, 0, reason: '${result.stdout}${result.stderr}');
  });
}
