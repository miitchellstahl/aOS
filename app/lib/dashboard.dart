import 'dart:async';
import 'dart:ui' as ui;

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
      scaffoldBackgroundColor: const Color(0xff080f1b),
      colorScheme: ColorScheme.fromSeed(
        seedColor: const Color(0xfff5a623),
        brightness: Brightness.dark,
      ),
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
                  child: BackdropGroup(
                    child: Stack(
                      children: [
                        const Positioned.fill(
                          child: RepaintBoundary(
                            child: CustomPaint(painter: _AtmospherePainter()),
                          ),
                        ),
                        Padding(
                          padding: const EdgeInsets.fromLTRB(64, 44, 64, 44),
                          child: FocusTraversalGroup(
                            descendantsAreTraversable:
                                controller.overlay == null,
                            policy: ReadingOrderTraversalPolicy(),
                            child: Column(
                              children: [
                                _header(accent),
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
                                  flex: 10,
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
                                  height: 176,
                                  child: _statusPanel(accent),
                                ),
                              ],
                            ),
                          ),
                        ),
                        if (controller.overlay != null)
                          Positioned.fill(
                            child: FocusScope(child: _notification(accent)),
                          ),
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

  Widget _header(Color accent) => SizedBox(
    height: 76,
    child: Row(
      children: [
        Expanded(
          child: Row(
            children: [
              const Text(
                'aOS',
                style: TextStyle(
                  fontSize: 40,
                  letterSpacing: -2,
                  fontWeight: FontWeight.w600,
                  color: Colors.white,
                ),
              ),
              const SizedBox(width: 24),
              Container(
                width: 1,
                height: 26,
                color: Colors.white.withValues(alpha: 0.25),
              ),
              const SizedBox(width: 24),
              const Expanded(
                child: Text(
                  'Your day, at a glance.',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(fontSize: 25, color: Color(0xffd4dfe9)),
                ),
              ),
            ],
          ),
        ),
        const SizedBox(width: 32),
        _GlassSurface(
          radius: 32,
          child: Padding(
            padding: const EdgeInsets.symmetric(horizontal: 24, vertical: 12),
            child: Row(
              mainAxisSize: MainAxisSize.min,
              children: [
                Icon(Icons.home_rounded, color: accent, size: 26),
                const SizedBox(width: 12),
                const Text(
                  'Home',
                  style: TextStyle(fontSize: 25, fontWeight: FontWeight.w600),
                ),
              ],
            ),
          ),
        ),
        const SizedBox(width: 32),
        const Expanded(
          child: Row(
            mainAxisAlignment: MainAxisAlignment.end,
            children: [
              Icon(Icons.open_with_rounded, size: 22, color: Color(0xffd4dfe9)),
              SizedBox(width: 12),
              Flexible(
                child: Text(
                  'Navigate   ·   Enter to select   ·   Esc to go back',
                  maxLines: 1,
                  overflow: TextOverflow.ellipsis,
                  style: TextStyle(fontSize: 20, color: Color(0xffd4dfe9)),
                ),
              ),
            ],
          ),
        ),
      ],
    ),
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
      icon: switch (type) {
        'clock' => Icons.schedule_rounded,
        'weather' => Icons.thermostat_rounded,
        'agenda' => Icons.calendar_today_rounded,
        'briefing' => Icons.auto_awesome_outlined,
        _ => null,
      },
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
            style: const TextStyle(fontSize: 32, color: Color(0xffd4dfe9)),
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
          maxLines: 1,
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
                        color: Color(0xffbecbd9),
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
      icon: Icons.check_circle_outline_rounded,
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
                        color: done ? accent : const Color(0xffbecbd9),
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
                                    ? const Color(0xffbecbd9)
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
                style: const TextStyle(fontSize: 30, color: Color(0xffd4dfe9)),
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
                style: const TextStyle(fontSize: 24, color: Color(0xffbecbd9)),
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
      style: const TextStyle(fontSize: 32, color: Color(0xffbecbd9)),
    ),
  );

  Widget _notification(Color accent) => ColoredBox(
    color: const Color(0x99080f1b),
    child: Center(
      child: SizedBox(
        width: 1100,
        child: _Panel(
          title: 'NOTIFICATION',
          icon: Icons.notifications_none_rounded,
          grouped: false,
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
                  backgroundColor: Colors.white.withValues(alpha: 0.1),
                  shape: const StadiumBorder(),
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
    this.icon,
    this.grouped = true,
  });
  final String title;
  final Color accent;
  final Widget child;
  final IconData? icon;
  final bool grouped;

  @override
  Widget build(BuildContext context) => _GlassSurface(
    grouped: grouped,
    child: Padding(
      padding: const EdgeInsets.all(28),
      child: Column(
        crossAxisAlignment: CrossAxisAlignment.start,
        mainAxisSize: MainAxisSize.min,
        children: [
          if (title.isNotEmpty) ...[
            Row(
              children: [
                if (icon != null) ...[
                  Icon(icon, size: 24, color: accent),
                  const SizedBox(width: 12),
                ],
                Flexible(
                  child: Text(
                    title,
                    maxLines: 1,
                    overflow: TextOverflow.ellipsis,
                    style: const TextStyle(
                      fontSize: 22,
                      letterSpacing: 2.4,
                      fontWeight: FontWeight.w600,
                      color: Color(0xffd4dfe9),
                    ),
                  ),
                ),
              ],
            ),
            const SizedBox(height: 16),
          ],
          Flexible(child: child),
        ],
      ),
    ),
  );
}

class _GlassSurface extends StatelessWidget {
  const _GlassSurface({
    required this.child,
    this.radius = 36,
    this.grouped = true,
  });

  final Widget child;
  final double radius;
  final bool grouped;

  @override
  Widget build(BuildContext context) {
    final content = DecoratedBox(
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(radius),
        gradient: LinearGradient(
          begin: Alignment.topLeft,
          end: Alignment.bottomRight,
          colors: [
            Color.alphaBlend(
              Colors.white.withValues(alpha: 0.1),
              const Color(0x99101927),
            ),
            Color.alphaBlend(
              Colors.white.withValues(alpha: 0.025),
              const Color(0x99101927),
            ),
          ],
        ),
        border: Border.all(color: Colors.white.withValues(alpha: 0.22)),
      ),
      child: child,
    );
    return DecoratedBox(
      decoration: BoxDecoration(
        borderRadius: BorderRadius.circular(radius),
        boxShadow: const [
          BoxShadow(
            color: Color(0x40000000),
            blurRadius: 28,
            offset: Offset(0, 12),
          ),
        ],
      ),
      child: ClipRRect(
        borderRadius: BorderRadius.circular(radius),
        child: grouped
            ? BackdropFilter.grouped(
                filter: ui.ImageFilter.blur(sigmaX: 12, sigmaY: 12),
                child: content,
              )
            : BackdropFilter(
                filter: ui.ImageFilter.blur(sigmaX: 12, sigmaY: 12),
                child: content,
              ),
      ),
    );
  }
}

class _AtmospherePainter extends CustomPainter {
  const _AtmospherePainter();

  @override
  void paint(Canvas canvas, Size size) {
    final bounds = Offset.zero & size;
    canvas.drawRect(bounds, Paint()..color = const Color(0xff080f1b));
    for (final (center, radius, color) in [
      (const Alignment(-0.95, -0.8), 0.8, const Color(0xff315b76)),
      (const Alignment(0.85, -0.65), 0.65, const Color(0xff416560)),
      (const Alignment(0.35, 1.3), 0.9, const Color(0xff62433e)),
    ]) {
      canvas.drawRect(
        bounds,
        Paint()
          ..shader = RadialGradient(
            center: center,
            radius: radius,
            colors: [color, color.withValues(alpha: 0)],
          ).createShader(bounds),
      );
    }
    final horizon = Path()
      ..moveTo(0, size.height * 0.7)
      ..cubicTo(
        size.width * 0.35,
        size.height * 0.15,
        size.width * 0.65,
        size.height * 1.25,
        size.width,
        size.height * 0.45,
      )
      ..lineTo(size.width, size.height)
      ..lineTo(0, size.height)
      ..close();
    canvas.drawPath(
      horizon,
      Paint()
        ..shader = const LinearGradient(
          begin: Alignment.topCenter,
          end: Alignment.bottomCenter,
          colors: [Color(0x184ca5b4), Color(0x00132235)],
        ).createShader(bounds),
    );
  }

  @override
  bool shouldRepaint(_AtmospherePainter oldDelegate) => false;
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
  Widget build(BuildContext context) {
    final duration = MediaQuery.disableAnimationsOf(context)
        ? Duration.zero
        : const Duration(milliseconds: 180);
    return FocusableActionDetector(
      autofocus: widget.autofocus,
      onFocusChange: (value) {
        setState(() => focused = value);
        if (value) {
          Scrollable.ensureVisible(context, duration: duration, alignment: 0.5);
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
          child: AnimatedScale(
            scale: focused ? 1.025 : 1,
            duration: duration,
            curve: const Cubic(0.23, 1, 0.32, 1),
            child: AnimatedContainer(
              duration: duration,
              curve: const Cubic(0.23, 1, 0.32, 1),
              margin: const EdgeInsets.symmetric(horizontal: 6, vertical: 4),
              padding: const EdgeInsets.all(12),
              decoration: BoxDecoration(
                gradient: LinearGradient(
                  begin: Alignment.topLeft,
                  end: Alignment.bottomRight,
                  colors: focused
                      ? [
                          Colors.white.withValues(alpha: 0.2),
                          Colors.white.withValues(alpha: 0.07),
                        ]
                      : [Colors.transparent, Colors.transparent],
                ),
                borderRadius: BorderRadius.circular(20),
                border: Border.all(
                  width: 2,
                  color: focused
                      ? Colors.white.withValues(alpha: 0.85)
                      : Colors.transparent,
                ),
                boxShadow: focused
                    ? [
                        BoxShadow(
                          color: widget.accent.withValues(alpha: 0.12),
                          blurRadius: 18,
                        ),
                      ]
                    : [],
              ),
              child: widget.child,
            ),
          ),
        ),
      ),
    );
  }
}
