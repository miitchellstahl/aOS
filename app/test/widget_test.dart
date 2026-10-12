// Bootstrap tests deliberately do not launch a UI or browser.
import 'dart:convert';
import 'dart:io';

import 'package:flutter/services.dart';
import 'package:flutter_test/flutter_test.dart';

import 'package:aos_display/bootstrap.dart';

class _ContractBundle extends CachingAssetBundle {
  @override
  Future<ByteData> load(String key) async {
    final bytes = await File(key).readAsBytes();
    return ByteData.sublistView(bytes);
  }
}

void main() {
  late Directory temporary;
  setUp(() async {
    temporary = await Directory('.dart_tool').createTemp('boot-test-');
  });
  tearDown(() async => temporary.delete(recursive: true));

  test(
    'missing, corrupt and unsupported file boot use validated bundled fallback',
    () async {
      final path = '${temporary.path}/dashboard.json';
      final bundle = _ContractBundle();
      for (final bytes in [null, 'not JSON', '{"version":2}']) {
        if (bytes != null) await File(path).writeAsString(bytes);
        final boot = await loadBootState(path, bundle: bundle);
        expect(boot.dashboard.revision, 0);
        expect(boot.diagnostic, contains('bundled sample'));
        if (bytes != null) expect(await File(path).readAsString(), bytes);
      }
    },
  );

  test(
    'valid boot is read-only and retains the real persisted update time',
    () async {
      final path = '${temporary.path}/dashboard.json';
      final sample = jsonDecode(
        await File('assets/contracts/examples/dashboard.json').readAsString(),
      );
      sample['revision'] = 12;
      final bytes = jsonEncode(sample);
      await File(path).writeAsString(bytes);
      final boot = await loadBootState(path, bundle: _ContractBundle());
      expect(boot.dashboard.revision, 12);
      expect(boot.dashboard.updatedAt.toUtc(), DateTime.utc(2026, 10, 12, 19));
      expect(boot.diagnostic, isNull);
      expect(await File(path).readAsString(), bytes);
    },
  );
}
