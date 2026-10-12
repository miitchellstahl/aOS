import 'dart:async';

import 'package:flutter/material.dart';
import 'package:flutter/services.dart';

import 'display_controller.dart';

class DashboardApp extends StatelessWidget {
  const DashboardApp({super.key, required this.controller});
  final DisplayController controller;

  @override
  Widget build(BuildContext context) => MaterialApp(
    title: 'aOS',
    debugShowCheckedModeBanner: false,
    theme: ThemeData(
      brightness: Brightness.dark,
      scaffoldBackgroundColor: const Color(0xff101216),
      colorScheme: ColorScheme.fromSeed(
        seedColor: const Color(0xfff5a623),
        brightness: Brightness.dark,
      ),
      fontFamily: 'Roboto',
      textTheme: const TextTheme(
        bodyLarge: TextStyle(fontSize: 34, height: 1.25),
        bodyMedium: TextStyle(fontSize: 30, height: 1.3),
      ),
    ),
    home: DashboardScreen(controller: controller),
  );
}

class DashboardScreen extends StatefulWidget {
  const DashboardScreen({super.key, required this.controller});
  final DisplayController controller;

  @override
  State<DashboardScreen> createState() => _DashboardScreenState();
}

class _DashboardScreenState extends State<DashboardScreen> {
  late DateTime now;
  Timer? _clock;
  DisplayController get controller => widget.controller;

  @override
  void initState() {
    super.initState();
    now = DateTime.now();
    _clock = Timer.periodic(const Duration(seconds: 1), (_) {
      setState(() => now = DateTime.now());
    });
  }

  @override
  void dispose() {
    _clock?.cancel();
    super.dispose();
  }

  @override
  Widget build(BuildContext context) => ListenableBuilder(
    listenable: controller,
    builder: (context, _) {
      final theme = controller.dashboard.theme;
      final accent = Color(
        0xff000000 |
            int.parse((theme['accent'] as String).substring(1), radix: 16),
      );
      return CallbackShortcuts(
        bindings: {
          const SingleActivator(LogicalKeyboardKey.escape): () {
            if (controller.overlay != null) {
              unawaited(controller.dismissOverlay());
            } else {
              FocusScope.of(context).previousFocus();
            }
          },
          const SingleActivator(LogicalKeyboardKey.goBack): () {
            if (controller.overlay != null) {
              unawaited(controller.dismissOverlay());
            } else {
              FocusScope.of(context).previousFocus();
            }
          },
        },
        child: Scaffold(
          body: SizedBox.expand(
            child: FittedBox(
              fit: BoxFit.contain,
              child: SizedBox(
                width: 1920,
                height: 1080,
                child: MediaQuery(
                  data: MediaQuery.of(context).copyWith(
                    textScaler: TextScaler.linear(
                      (theme['font_scale'] as num).toDouble(),
                    ),
                  ),
                  child: DecoratedBox(
                    decoration: const BoxDecoration(
                      gradient: RadialGradient(
                        center: Alignment(-0.8, -0.9),
                        radius: 1.4,
                        colors: [Color(0xff30251c), Color(0xff101216)],
                      ),
                    ),
                    child: Stack(
                      children: [
                        Padding(
                          padding: const EdgeInsets.fromLTRB(64, 40, 64, 40),
                          child: FocusTraversalGroup(
                            descendantsAreFocusable: controller.overlay == null,
                            descendantsAreTraversable:
                                controller.overlay == null,
                            policy: ReadingOrderTraversalPolicy(),
                            child: Column(
                              children: [
                                Row(
                                  children: [
                                    Text(
                                      'aOS',
                                      style: TextStyle(
                                        fontSize: 32,
                                        fontWeight: FontWeight.w700,
                                        color: accent,
                                      ),
                                    ),
                                    const SizedBox(width: 20),
                                    const Text(
                                      'AT HOME',
                                      style: TextStyle(
                                        fontSize: 22,
                                        letterSpacing: 5,
                                        color: Color(0xffb4b1aa),
                                      ),
                                    ),
                                    const SizedBox(width: 32),
                                    const Expanded(
                                      child: Text(
                                        'ARROWS  navigate     ENTER  select     ESC  back',
                                        textAlign: TextAlign.right,
                                        maxLines: 1,
                                        overflow: TextOverflow.ellipsis,
                                        style: TextStyle(
                                          fontSize: 20,
                                          color: Color(0xffb4b1aa),
                                        ),
                                      ),
                                    ),
                                  ],
                                ),
                                const SizedBox(height: 28),
                                Expanded(
                                  flex: 6,
                                  child: Row(
                                    children: [
                                      Expanded(
                                        flex: 7,
                                        child: _clockPanel(accent),
                                      ),
                                      const SizedBox(width: 28),
                                      Expanded(
                                        flex: 3,
                                        child: _weatherPanel(accent),
                                      ),
                                    ],
                                  ),
                                ),
                                const SizedBox(height: 28),
                                Expanded(
                                  flex: 9,
                                  child: Row(
                                    crossAxisAlignment:
                                        CrossAxisAlignment.stretch,
                                    children: [
                                      Expanded(child: _agendaPanel(accent)),
                                      const SizedBox(width: 28),
                                      Expanded(child: _todosPanel(accent)),
                                      const SizedBox(width: 28),
                                      Expanded(child: _briefingPanel(accent)),
                                    ],
                                  ),
                                ),
                                const SizedBox(height: 28),
                                SizedBox(
                                  height: 190,
                                  child: _statusPanel(accent),
                                ),
                              ],
                            ),
                          ),
                        ),
                        if (controller.overlay != null)
                          Positioned.fill(child: _notification(accent)),
                      ],
                    ),
                  ),
                ),
              ),
            ),
          ),
        ),
      );
    },
  );

  Widget _panel(
    String type,
    String title,
    Color accent,
    Widget Function(Map<String, Object?>) body,
  ) {
    final section = controller.dashboard.section(type);
    return _Panel(
      key: ValueKey<String>(section?['id'] as String? ?? 'empty-$type'),
      title: section == null ? '' : title,
      accent: accent,
      child: section == null ? const SizedBox.expand() : body(section),
    );
  }

  Widget _clockPanel(Color accent) => _panel('clock', 'LOCAL TIME', accent, (
    section,
  ) {
    final is12 = section['format'] == '12h';
    final hour = is12 ? (now.hour % 12 == 0 ? 12 : now.hour % 12) : now.hour;
    const weekdays = [
      'Monday',
      'Tuesday',
      'Wednesday',
      'Thursday',
      'Friday',
      'Saturday',
      'Sunday',
    ];
    const months = [
      'January',
      'February',
      'March',
      'April',
      'May',
      'June',
      'July',
      'August',
      'September',
      'October',
      'November',
      'December',
    ];
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Expanded(
          child: Align(
            alignment: Alignment.centerLeft,
            child: FittedBox(
              fit: BoxFit.scaleDown,
              child: Row(
                crossAxisAlignment: CrossAxisAlignment.baseline,
                textBaseline: TextBaseline.alphabetic,
                children: [
                  Text(
                    '${hour.toString().padLeft(is12 ? 1 : 2, '0')}:${now.minute.toString().padLeft(2, '0')}',
                    style: const TextStyle(
                      fontSize: 172,
                      fontWeight: FontWeight.w300,
                      height: 1,
                      letterSpacing: -7,
                      fontFeatures: [FontFeature.tabularFigures()],
                    ),
                  ),
                  if (is12)
                    Text(
                      now.hour < 12 ? '  AM' : '  PM',
                      style: TextStyle(fontSize: 38, color: accent),
                    ),
                ],
              ),
            ),
          ),
        ),
        if (section['show_date'] == true)
          Text(
            '${weekdays[now.weekday - 1]}, ${months[now.month - 1]} ${now.day}',
            maxLines: 1,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(fontSize: 34, color: Color(0xffc7c2b9)),
          ),
      ],
    );
  });

  Widget _weatherPanel(Color accent) => _panel(
    'weather',
    'OUTSIDE',
    accent,
    (section) => Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      children: [
        Expanded(
          child: Align(
            alignment: Alignment.centerLeft,
            child: FittedBox(
              fit: BoxFit.scaleDown,
              child: Text(
                '${(section['temp_f'] as num).round()}°F',
                style: TextStyle(
                  fontSize: 98,
                  fontWeight: FontWeight.w300,
                  color: accent,
                ),
              ),
            ),
          ),
        ),
        Text(
          section['summary'] as String,
          maxLines: 2,
          overflow: TextOverflow.ellipsis,
          style: const TextStyle(fontSize: 32),
        ),
      ],
    ),
  );

  Widget _agendaPanel(Color accent) =>
      _panel('agenda', 'AGENDA', accent, (section) {
        final events = section['events'] as List;
        if (events.isEmpty) return _quiet('Room to breathe.');
        return ListView.separated(
          itemCount: events.length,
          separatorBuilder: (_, _) => const SizedBox(height: 20),
          itemBuilder: (context, i) {
            final event = events[i] as Map;
            return _FocusableRow(
              accent: accent,
              child: Column(
                crossAxisAlignment: CrossAxisAlignment.start,
                children: [
                  Text(
                    event['time'] as String,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: TextStyle(fontSize: 28, color: accent),
                  ),
                  const SizedBox(height: 6),
                  Text(
                    event['title'] as String,
                    maxLines: 2,
                    overflow: TextOverflow.ellipsis,
                  ),
                  if (event['where'] != null)
                    Text(
                      event['where'] as String,
                      maxLines: 1,
                      overflow: TextOverflow.ellipsis,
                      style: const TextStyle(
                        fontSize: 26,
                        color: Color(0xffb4b1aa),
                      ),
                    ),
                ],
              ),
            );
          },
        );
      });

  Widget _todosPanel(Color accent) {
    final sections = controller.dashboard.sections
        .where((s) => s['type'] == 'todos')
        .toList();
    final rows = [
      for (final section in sections)
        for (final item in section['items'] as List)
          (section['id'] as String, item as Map),
    ];
    return _Panel(
      key: const ValueKey('todos-slot'),
      title: sections.isEmpty ? '' : 'TO DO',
      accent: accent,
      child: sections.isEmpty
          ? const SizedBox.expand()
          : rows.isEmpty
          ? _quiet('All clear.')
          : ListView.separated(
              itemCount: rows.length,
              separatorBuilder: (_, _) => const SizedBox(height: 14),
              itemBuilder: (context, i) {
                final (sectionId, item) = rows[i];
                final id = item['id'] as String;
                final done = item['done'] as bool;
                final pending =
                    controller.pending[controller.todoKey(sectionId, id)];
                return _FocusableRow(
                  key: ValueKey(controller.todoKey(sectionId, id)),
                  autofocus: i == 0,
                  accent: accent,
                  onActivate: () => unawaited(controller.toggle(sectionId, id)),
                  child: Row(
                    crossAxisAlignment: CrossAxisAlignment.start,
                    children: [
                      Icon(
                        done
                            ? Icons.check_circle
                            : Icons.radio_button_unchecked,
                        size: 34,
                        color: done ? accent : const Color(0xffb4b1aa),
                      ),
                      const SizedBox(width: 18),
                      Expanded(
                        child: Column(
                          crossAxisAlignment: CrossAxisAlignment.start,
                          children: [
                            Text(
                              item['label'] as String,
                              maxLines: 2,
                              overflow: TextOverflow.ellipsis,
                              style: TextStyle(
                                fontSize: 32,
                                color: done
                                    ? const Color(0xffb4b1aa)
                                    : Colors.white,
                                decoration: done
                                    ? TextDecoration.lineThrough
                                    : null,
                              ),
                            ),
                            if (pending != null)
                              Text(
                                pending.failed
                                    ? 'Not confirmed · Enter to retry'
                                    : 'Pending agent confirmation',
                                style: TextStyle(fontSize: 22, color: accent),
                              ),
                          ],
                        ),
                      ),
                    ],
                  ),
                );
              },
            ),
    );
  }

  Widget _briefingPanel(Color accent) => _panel(
    'briefing',
    'BRIEFING',
    accent,
    (section) => ListView(
      children: [
        _FocusableRow(
          accent: accent,
          child: Text(
            section['headline'] as String,
            maxLines: 3,
            overflow: TextOverflow.ellipsis,
            style: const TextStyle(fontSize: 36, fontWeight: FontWeight.w500),
          ),
        ),
        const SizedBox(height: 18),
        for (final item in section['items'] as List)
          Padding(
            padding: const EdgeInsets.only(bottom: 18),
            child: _FocusableRow(
              accent: accent,
              child: Text(
                '—  $item',
                maxLines: 4,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(fontSize: 30, color: Color(0xffd0cbc3)),
              ),
            ),
          ),
      ],
    ),
  );

  Widget _statusPanel(Color accent) => _panel('status', '', accent, (section) {
    final elapsed = now.toUtc().difference(
      controller.dashboard.updatedAt.toUtc(),
    );
    final age = elapsed.isNegative
        ? 'timestamp ahead of local clock'
        : elapsed.inDays > 0
        ? '${elapsed.inDays}d ${elapsed.inHours % 24}h ago'
        : elapsed.inHours > 0
        ? '${elapsed.inHours}h ${elapsed.inMinutes % 60}m ago'
        : elapsed.inMinutes > 0
        ? '${elapsed.inMinutes}m ago'
        : '${elapsed.inSeconds}s ago';
    final diagnostic =
        controller.inputDiagnostic ??
        controller.streamDiagnostic ??
        (controller.connection == 'Live' ? null : controller.bootDiagnostic);
    return Column(
      crossAxisAlignment: CrossAxisAlignment.start,
      mainAxisAlignment: MainAxisAlignment.center,
      children: [
        Row(
          children: [
            Icon(
              Icons.circle,
              size: 16,
              color: controller.connection == 'Live'
                  ? const Color(0xffa2c4a0)
                  : accent,
            ),
            const SizedBox(width: 14),
            Text(
              'Bridge · ${controller.connection}',
              style: const TextStyle(fontSize: 26),
            ),
            const SizedBox(width: 32),
            Expanded(
              child: Text(
                'Dashboard updated $age   ·   r${controller.dashboard.revision}',
                textAlign: TextAlign.right,
                maxLines: 1,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(fontSize: 24, color: Color(0xffb4b1aa)),
              ),
            ),
          ],
        ),
        const SizedBox(height: 10),
        Text(
          diagnostic ?? section['note'] as String,
          maxLines: 1,
          overflow: TextOverflow.ellipsis,
          style: TextStyle(
            fontSize: 24,
            color: diagnostic == null ? Colors.white : accent,
          ),
        ),
      ],
    );
  });

  Widget _quiet(String text) => Align(
    alignment: Alignment.topLeft,
    child: Text(
      text,
      style: const TextStyle(fontSize: 32, color: Color(0xffb4b1aa)),
    ),
  );

  Widget _notification(Color accent) => ColoredBox(
    color: const Color(0xcc08090b),
    child: Center(
      child: SizedBox(
        width: 1100,
        child: _Panel(
          title: 'NOTIFICATION',
          accent: accent,
          child: Column(
            mainAxisSize: MainAxisSize.min,
            children: [
              Text(
                controller.overlay!,
                maxLines: 6,
                overflow: TextOverflow.ellipsis,
                style: const TextStyle(fontSize: 42, height: 1.3),
              ),
              const SizedBox(height: 32),
              TextButton(
                autofocus: true,
                onPressed: () => unawaited(controller.dismissOverlay()),
                style: TextButton.styleFrom(
                  foregroundColor: accent,
                  textStyle: const TextStyle(fontSize: 30),
                  padding: const EdgeInsets.symmetric(
                    horizontal: 36,
                    vertical: 18,
                  ),
                ),
                child: const Text('Dismiss · Enter or Escape'),
              ),
            ],
          ),
        ),
      ),
    ),
  );
}

class _Panel extends StatelessWidget {
  const _Panel({
    super.key,
    required this.title,
    required this.accent,
    required this.child,
  });
  final String title;
  final Color accent;
  final Widget child;

  @override
  Widget build(BuildContext context) => DecoratedBox(
    decoration: BoxDecoration(
      color: const Color(0xe61b1d21),
      borderRadius: BorderRadius.circular(24),
      border: Border.all(color: const Color(0xff393735)),
    ),
    child: Padding(
      padding: const EdgeInsets.all(28),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          if (title.isNotEmpty) ...[
            Text(
              title,
              style: TextStyle(
                fontSize: 23,
                letterSpacing: 3,
                fontWeight: FontWeight.w600,
                color: accent,
              ),
            ),
            const SizedBox(height: 16),
          ],
          Flexible(child: child),
        ],
      ),
    ),
  );
}

class _FocusableRow extends StatefulWidget {
  const _FocusableRow({
    super.key,
    required this.child,
    required this.accent,
    this.onActivate,
    this.autofocus = false,
  });
  final Widget child;
  final Color accent;
  final VoidCallback? onActivate;
  final bool autofocus;

  @override
  State<_FocusableRow> createState() => _FocusableRowState();
}

class _FocusableRowState extends State<_FocusableRow> {
  bool focused = false;

  @override
  Widget build(BuildContext context) => FocusableActionDetector(
    autofocus: widget.autofocus,
    onFocusChange: (value) {
      setState(() => focused = value);
      if (value) {
        Scrollable.ensureVisible(
          context,
          duration: const Duration(milliseconds: 150),
          alignment: 0.5,
        );
      }
    },
    shortcuts: const {
      SingleActivator(LogicalKeyboardKey.enter): ActivateIntent(),
      SingleActivator(LogicalKeyboardKey.numpadEnter): ActivateIntent(),
      SingleActivator(LogicalKeyboardKey.select): ActivateIntent(),
      SingleActivator(LogicalKeyboardKey.space): ActivateIntent(),
    },
    actions: {
      ActivateIntent: CallbackAction<ActivateIntent>(
        onInvoke: (_) {
          widget.onActivate?.call();
          return null;
        },
      ),
    },
    child: Semantics(
      button: widget.onActivate != null,
      focused: focused,
      child: GestureDetector(
        onTap: widget.onActivate,
        child: AnimatedContainer(
          duration: const Duration(milliseconds: 120),
          padding: const EdgeInsets.all(12),
          decoration: BoxDecoration(
            color: focused
                ? widget.accent.withValues(alpha: 0.08)
                : Colors.transparent,
            borderRadius: BorderRadius.circular(12),
            border: Border.all(
              width: 2,
              color: focused ? widget.accent : Colors.transparent,
            ),
          ),
          child: widget.child,
        ),
      ),
    ),
  );
}
