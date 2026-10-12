import 'dart:convert';
import 'dart:io';

import 'package:flutter/services.dart';

import 'contracts.dart';

class BootState {
  BootState(this.contracts, this.dashboard, this.diagnostic);
  final Contracts contracts;
  final Dashboard dashboard;
  final String? diagnostic;
}

Future<BootState> loadBootState(
  String snapshotPath, {
  AssetBundle? bundle,
}) async {
  final assets = bundle ?? rootBundle;
  Future<Object?> load(String name) async =>
      jsonDecode(await assets.loadString('assets/contracts/$name'));
  final contracts = Contracts(
    Map<String, Object?>.from(await load('dashboard.v1.schema.json') as Map),
    await load('input.v1.schema.json'),
    await load('custom-events.schema.json'),
    await load('ag-ui/schema.json'),
  );
  // validate the bundled fallback too: a packaging/contract error must not pass
  // as a valid snapshot. This never writes the bridge-owned dashboard file.
  final fallback = contracts.dashboard(await load('examples/dashboard.json'));
  try {
    final file = File(snapshotPath);
    if (await file.length() > Contracts.maxBytes) {
      throw const FormatException('Boot snapshot exceeds bounds');
    }
    final bytes = await file.readAsBytes();
    if (bytes.length > Contracts.maxBytes) {
      throw const FormatException('Boot snapshot exceeds bounds');
    }
    return BootState(
      contracts,
      contracts.dashboard(jsonDecode(utf8.decode(bytes))),
      null,
    );
  } catch (_) {
    return BootState(
      contracts,
      fallback,
      'Boot snapshot unavailable or invalid · showing bundled sample',
    );
  }
}
