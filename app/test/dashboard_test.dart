import 'dart:convert';

import 'package:flutter/material.dart';
import 'package:flutter/rendering.dart';
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
  testWidgets('glass panels share a blur pass, but the modal is independent', (
    tester,
  ) async {
    final contracts = loadContracts();
    final controller = RecordingController(
      contracts,
      contracts.dashboard(fixture('examples/dashboard.json')),
    );
    await tester.pumpWidget(DashboardApp(controller: controller));
    await tester.pump();
    final filters = find
        .byType(BackdropFilter)
        .evaluate()
        .map((element) => element.renderObject! as RenderBackdropFilter);
    expect(filters, hasLength(7));
    final dashboardKey = filters.first.backdropKey;
    expect(dashboardKey, isNotNull);
    expect(
      filters.every((filter) => filter.backdropKey == dashboardKey),
      isTrue,
    );

    controller.acceptFrame(jsonEncode(fixture('examples/snapshot.json')));
    controller.acceptFrame(jsonEncode(fixture('examples/overlay.json')));
    await tester.pump();
    final modalFilter = tester.renderObject<RenderBackdropFilter>(
      find.descendant(
        of: find
            .ancestor(
              of: find.text('NOTIFICATION'),
              matching: find.byType(ClipRRect),
            )
            .first,
        matching: find.byType(BackdropFilter),
      ),
    );
    expect(modalFilter.backdropKey, isNot(dashboardKey));
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox());
    controller.dispose();
  });

  for (final reducedMotion in [false, true]) {
    testWidgets('remote focus lift respects reduced motion: $reducedMotion', (
      tester,
    ) async {
      tester.platformDispatcher.accessibilityFeaturesTestValue =
          FakeAccessibilityFeatures(disableAnimations: reducedMotion);
      addTearDown(
        tester.platformDispatcher.clearAccessibilityFeaturesTestValue,
      );
      final contracts = loadContracts();
      final controller = RecordingController(
        contracts,
        contracts.dashboard(fixture('examples/dashboard.json')),
      );
      await tester.pumpWidget(DashboardApp(controller: controller));
      await tester.pump();
      AnimatedScale lift(String id) => tester.widget<AnimatedScale>(
        find.descendant(
          of: find.byKey(ValueKey('todos/$id')),
          matching: find.byType(AnimatedScale),
        ),
      );
      expect(lift('t1').scale, 1.025);
      expect(
        lift('t1').duration,
        reducedMotion ? Duration.zero : const Duration(milliseconds: 180),
      );
      await tester.sendKeyEvent(LogicalKeyboardKey.arrowDown);
      await tester.pump(const Duration(milliseconds: 200));
      expect(lift('t1').scale, 1);
      expect(lift('t2').scale, 1.025);
      expect(tester.takeException(), isNull);
      await tester.pumpWidget(const SizedBox());
      controller.dispose();
    });
  }

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

  testWidgets('large weather text leaves a readable temperature, not a speck', (
    tester,
  ) async {
    final contracts = loadContracts();
    final data = fixture('examples/dashboard.json') as Map;
    data['theme']['font_scale'] = 1.5;
    final weather =
        (data['sections'] as List).firstWhere(
              (section) => section['type'] == 'weather',
            )
            as Map;
    weather['temp_f'] = 64;
    weather['summary'] = 'Light rain this evening';
    final controller = RecordingController(
      contracts,
      contracts.dashboard(data),
    );
    await tester.binding.setSurfaceSize(const Size(1920, 1080));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    await tester.pumpWidget(DashboardApp(controller: controller));
    await tester.pump();
    final temperature = find.text('64°F');
    final paintedHeight =
        tester.getBottomRight(temperature).dy -
        tester.getTopLeft(temperature).dy;
    expect(paintedHeight, greaterThanOrEqualTo(48));
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox());
    controller.dispose();
  });

  testWidgets('large briefing text scrolls fully into view when focused', (
    tester,
  ) async {
    final contracts = loadContracts();
    final data = fixture('examples/dashboard.json') as Map;
    data['theme']['font_scale'] = 1.5;
    final briefing =
        (data['sections'] as List).firstWhere(
              (section) => section['type'] == 'briefing',
            )
            as Map;
    briefing['headline'] = 'Evening plans are ready';
    const body = 'Dinner at 7. Bring an umbrella for the walk home.';
    briefing['items'] = [body];
    final controller = RecordingController(
      contracts,
      contracts.dashboard(data),
    );
    await tester.binding.setSurfaceSize(const Size(1920, 1080));
    addTearDown(() => tester.binding.setSurfaceSize(null));
    await tester.pumpWidget(DashboardApp(controller: controller));
    await tester.pump();
    final item = find.text('—  $body');
    Focus.of(tester.element(item)).requestFocus();
    await tester.pump();
    await tester.pump(const Duration(milliseconds: 200));
    final viewport = find.ancestor(of: item, matching: find.byType(ListView));
    expect(
      tester.getTopLeft(item).dy,
      greaterThanOrEqualTo(tester.getTopLeft(viewport).dy),
    );
    expect(
      tester.getBottomRight(item).dy,
      lessThanOrEqualTo(tester.getBottomRight(viewport).dy),
    );
    expect(tester.takeException(), isNull);
    await tester.pumpWidget(const SizedBox());
    controller.dispose();
  });
}
