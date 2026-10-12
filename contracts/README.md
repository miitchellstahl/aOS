# Shared contracts

JSON Schema draft 2020-12 owns state, custom events, and UI intents. The same
positive/negative fixtures must be checked in Python and Dart. Known sections are
strict; unknown types have bounded data and render as empty slots.

`ag-ui/` contains the upstream schema, provenance, and license. The appliance's
authoring ingress accepts only STATE_SNAPSHOT, STATE_DELTA, and supported CUSTOM
commands. The display feed is snapshot-first and runless: it is not a fully
conformant AG-UI run transport, and does not provide an A2UI renderer.
