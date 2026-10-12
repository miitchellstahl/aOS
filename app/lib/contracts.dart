import 'dart:convert';

import 'package:json_schema/json_schema.dart';
import 'package:rfc_6902/rfc_6902.dart';

/// Validates the shared, bundled schemas without maintaining Dart schema copies.
class Contracts {
  Contracts(
    Map<String, Object?> dashboard,
    Object? input,
    Object? custom,
    Object? events,
  ) : dashboardSchema = JsonSchema.create(dashboard),
      inputSchema = JsonSchema.create(input!),
      customSchema = JsonSchema.create(custom!),
      eventSchema = JsonSchema.create(events!) {
    final sections = dashboard['properties'] as Map;
    final branches =
        ((sections['sections'] as Map)['items'] as Map)['oneOf'] as List;
    knownTypes = branches
        .map((branch) => ((branch as Map)['properties'] as Map)['type'] as Map)
        .where((type) => type.containsKey('const'))
        .map((type) => type['const'] as String)
        .toSet();
  }

  final JsonSchema dashboardSchema;
  final JsonSchema inputSchema;
  final JsonSchema customSchema;
  final JsonSchema eventSchema;
  late final Set<String> knownTypes;
  static const maxBytes = 128 * 1024;
  static const maxEventBytes = maxBytes + 1024;

  void _validate(JsonSchema schema, Object? value) {
    final result = schema.validate(value, validateFormats: true);
    if (!result.isValid) {
      throw const FormatException('Payload does not match the shared contract');
    }
  }

  Dashboard dashboard(Object? value) {
    if (utf8.encode(jsonEncode(value)).length > maxBytes) {
      throw const FormatException('Dashboard exceeds 128 KiB');
    }
    _validate(dashboardSchema, value);
    final data = Map<String, Object?>.from(value as Map);
    _validateTimestamp(data['updated_at'] as String);
    final sections = (data['sections'] as List)
        .map((section) => Map<String, Object?>.from(section as Map))
        .toList();
    final ids = <String>{};
    final types = <String>{};
    for (final section in sections) {
      if (!ids.add(section['id'] as String)) {
        throw const FormatException('Duplicate section ID');
      }
      final type = section['type'] as String;
      // todos may have multiple collections; all other known renderers are singular.
      if (knownTypes.contains(type) && type != 'todos' && !types.add(type)) {
        throw const FormatException('Duplicate singular section type');
      }
      if (type == 'todos') {
        final todoIds = <String>{};
        for (final item in section['items'] as List) {
          if (!todoIds.add((item as Map)['id'] as String)) {
            throw const FormatException('Duplicate todo ID');
          }
        }
      }
    }
    return Dashboard._(data);
  }

  void _validateTimestamp(String value) {
    // json_schema 5.2.2 uses DateTime.parse for date-time, which normalizes
    // impossible dates and accepts absent zones. Reject those explicitly.
    final match = RegExp(
      r'^(\d{4})-(\d{2})-(\d{2})[Tt](\d{2}):(\d{2}):(\d{2})(?:\.\d+)?(?:[Zz]|([+-])(\d{2}):(\d{2}))$',
    ).firstMatch(value);
    if (match == null) {
      throw const FormatException('Invalid RFC 3339 timestamp');
    }
    final parts = List.generate(6, (i) => int.parse(match.group(i + 1)!));
    final date = DateTime.utc(parts[0], parts[1], parts[2]);
    if (date.year != parts[0] ||
        date.month != parts[1] ||
        date.day != parts[2] ||
        parts[3] > 23 ||
        parts[4] > 59 ||
        parts[5] > 59 ||
        (match.group(8) != null &&
            (int.parse(match.group(8)!) > 23 ||
                int.parse(match.group(9)!) > 59))) {
      throw const FormatException('Invalid RFC 3339 timestamp');
    }
  }

  void input(Object? value) => _validate(inputSchema, value);
  void custom(Object? value) => _validate(customSchema, value);
  void event(Object? value) {
    if (utf8.encode(jsonEncode(value)).length > maxEventBytes) {
      throw const FormatException('Event exceeds 129 KiB');
    }
    _validate(eventSchema, value);
  }

  /// Applies a committed bridge delta to a copy; validation precedes publication.
  Dashboard apply(Dashboard previous, List<dynamic> operations) {
    if (operations.length > 128 ||
        utf8.encode(jsonEncode(operations)).length > maxEventBytes) {
      throw const FormatException('Delta exceeds bounds');
    }
    // RFC 6902 tests are enforced by the patch library. Sequential output
    // revision also guards the shared delta fixture, which has no leading test.
    final copy = jsonDecode(jsonEncode(previous.data));
    final next = dashboard(JsonPatch(operations).applyTo(copy));
    if (next.revision != previous.revision + 1) {
      throw const FormatException('Non-sequential bridge revision');
    }
    return next;
  }
}

class Dashboard {
  Dashboard._(Map<String, Object?> data)
    : data = _freeze(data) as Map<String, Object?>;

  final Map<String, Object?> data;
  int get revision => data['revision'] as int;
  DateTime get updatedAt => DateTime.parse(data['updated_at'] as String);
  Map<String, Object?> get theme => data['theme'] as Map<String, Object?>;
  List<Map<String, Object?>> get sections =>
      (data['sections'] as List).cast<Map<String, Object?>>();

  Map<String, Object?>? section(String type) {
    for (final section in sections) {
      if (section['type'] == type) return section;
    }
    return null;
  }

  static Object? _freeze(Object? value) {
    if (value is Map) {
      return Map<String, Object?>.unmodifiable(
        value.map((key, value) => MapEntry(key as String, _freeze(value))),
      );
    }
    if (value is List) return List<Object?>.unmodifiable(value.map(_freeze));
    return value;
  }
}
