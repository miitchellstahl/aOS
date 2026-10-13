import 'package:flutter/material.dart';

import 'bootstrap.dart';
import 'bridge_client.dart';
import 'dashboard.dart';
import 'display_controller.dart';

Future<void> main() async {
  WidgetsFlutterBinding.ensureInitialized();
  try {
    final config = DisplayConfig.environment();
    final boot = await loadBootState(config.dashboardPath);
    final controller = DisplayController(
      contracts: boot.contracts,
      client: BridgeClient(config),
      dashboard: boot.dashboard,
      bootDiagnostic: boot.diagnostic,
    );
    runApp(DashboardApp(controller: controller));
    controller.start();
  } catch (_) {
    // fail visibly and closed on invalid config or missing contract assets.
    runApp(
      const MaterialApp(
        debugShowCheckedModeBanner: false,
        home: Scaffold(
          backgroundColor: Color(0xff101216),
          body: Center(
            child: Text(
              'aOS display could not start.\nCheck runtime configuration and bundled contracts.',
              textAlign: TextAlign.center,
              style: TextStyle(color: Colors.white, fontSize: 32),
            ),
          ),
        ),
      ),
    );
  }
}
