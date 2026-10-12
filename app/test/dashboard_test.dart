import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:aos_display/bridge_client.dart';
import 'package:aos_display/contracts.dart';
import 'package:aos_display/dashboard.dart';
import 'package:aos_display/display_controller.dart';

import 'contracts_test.dart' show fixture, loadContracts;

class RecordingController extends DisplayController {
  RecordingController(Contracts contracts, Dashboard dashboard)
    : super(
        contracts: contracts,
        dashboard: dashboard,
        client: BridgeClient(
          DisplayConfig(
            bridgeUrl: Uri.parse('http://127.0.0.1:8080'),
            dashboardPath: 'unused',
            tokenPath: 'unused',
          ),
        ),
      );
  final activated = <String>[];
  int dismissed = 0;
  @override
  Future<void> toggle(String sectionId, String targetId) async {
    activated.add(todoKey(sectionId, targetId));
  }

  @override
  Future<void> dismissOverlay() async {
    dismissed++;
    overlay = null;
    notifyListeners();
  }
}

void main() {
  testWidgets(
    'six native sections render without layout errors and D-pad/Enter focus works',
    (tester) async {
      final contracts = loadContracts();
      final controller = RecordingController(
        contracts,
        contracts.dashboard(fixture('examples/dashboard.json')),
      );
      await tester.pumpWidget(DashboardApp(controller: controller));
      await tester.pump();
      for (final label in [
        'LOCAL TIME',
        'OUTSIDE',
        'AGENDA',
        'TO DO',
        'BRIEFING',
      ]) {
        expect(find.text(label), findsOneWidget);
      }
      expect(find.textContaining('Dashboard updated'), findsOneWidget);
      expect(find.text('Bridge · Offline'), findsOneWidget);
      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
      expect(controller.activated, ['todos/t1']);
      final initial = FocusManager.instance.primaryFocus;
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
      await tester.pump();
      expect(FocusManager.instance.primaryFocus, isNot(initial));
      await tester.sendKeyEvent(LogicalKeyboardKey.enter);
      expect(controller.activated.last, 'todos/t2');
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
      controller.dispose();
    },
  );

  testWidgets(
    'unknown section keeps an empty fixed slot; IDs retain todo targets',
    (tester) async {
      final contracts = loadContracts();
      final data = fixture('examples/dashboard.json') as Map;
      data['sections'][1] = {
        'id': 'future-slot',
        'type': 'future',
        'content': '<div>not HTML</div>',
      };
      final controller = RecordingController(
        contracts,
        contracts.dashboard(data),
      );
      await tester.pumpWidget(DashboardApp(controller: controller));
      await tester.pump();
      expect(find.byKey(const ValueKey('empty-agenda')), findsOneWidget);
      expect(find.text('AGENDA'), findsNothing);
      expect(find.text('<div>not HTML</div>'), findsNothing);
      expect(find.byKey(const ValueKey('todos/t1')), findsOneWidget);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
      controller.dispose();
    },
  );

  for (final dismissal in ['escape', 'enter', 'expiry']) {
    testWidgets(
      'overlay restores previous focus after $dismissal without activating underlying todo',
      (tester) async {
        final contracts = loadContracts();
        final controller = RecordingController(
          contracts,
          contracts.dashboard(fixture('examples/dashboard.json')),
        );
        await tester.pumpWidget(DashboardApp(controller: controller));
        await tester.pump();
        final previousFocus = FocusManager.instance.primaryFocus;
        controller.acceptFrame(jsonEncode(fixture('examples/snapshot.json')));
        controller.acceptFrame(jsonEncode(fixture('examples/overlay.json')));
        await tester.pump();
        expect(find.text('Briefing ready'), findsOneWidget);
        final overlayFocus = FocusManager.instance.primaryFocus;
        expect(overlayFocus, isNot(previousFocus));
        for (final key in [
          LogicalKeyboardKey.arrowDown,
          LogicalKeyboardKey.arrowUp,
          LogicalKeyboardKey.arrowLeft,
          LogicalKeyboardKey.arrowRight,
        ]) {
          await tester.sendKeyEvent(key);
          await tester.pump();
          expect(FocusManager.instance.primaryFocus, overlayFocus);
        }
        if (dismissal == 'expiry') {
          controller.overlay = null;
          controller.notifyListeners();
        } else {
          await tester.sendKeyEvent(
            dismissal == 'escape'
                ? LogicalKeyboardKey.escape
                : LogicalKeyboardKey.enter,
          );
        }
        await tester.pump();
        await tester.pump();
        expect(controller.dismissed, dismissal == 'expiry' ? 0 : 1);
        expect(controller.activated, isEmpty);
        expect(controller.overlay, isNull);
        expect(FocusManager.instance.primaryFocus, previousFocus);
        await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
        await tester.pump();
        await tester.sendKeyEvent(LogicalKeyboardKey.enter);
        expect(controller.activated, ['todos/t2']);
        expect(tester.takeException(), isNull);
        await tester.pumpWidget(const SizedBox());
        controller.dispose();
      },
    );
  }

  testWidgets('maximum contract font scale remains bounded', (tester) async {
    final contracts = loadContracts();
    final data = fixture('examples/dashboard.json') as Map;
    data['theme']['font_scale'] = 1.5;
    final controller = RecordingController(
      contracts,
      contracts.dashboard(data),
    );
    await tester.pumpWidget(DashboardApp(controller: controller));
    await tester.pump();
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox());
    controller.dispose();
  });
}
