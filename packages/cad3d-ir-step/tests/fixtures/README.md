# Reference fixture provenance

`reference-assembly.step` is generated entirely from primitive boxes and
cylinders by `scripts/generate_reference_fixture.py`. It contains no external
CAD data and is covered by this repository's MIT license.

`native-reference.json` is an independent, typed synthetic native-CAD source
fixture describing the same assembly. `expected-semantics.json` pins the
format-independent expected projection. Neither file is a cad3d-ir manifest,
and the native fixture does not claim to model or parse SOLIDWORKS.

The semantic abstraction test compares both conversion results while excluding
source provenance, adapter-specific IDs, and representation bytes.

Pinned SHA-256 values:

- `reference-assembly.step`:
  `ce52a8347e020edfde44305023aa882f465adb2c621b33651cb8fa5da4d8e76b`
- `native-reference.json`:
  `892958ef605ca82d9dbabd1b4811c5ac133ed4bf8d4b9fdac59385aa864d3e14`
- `expected-semantics.json`:
  `0a92ea6da6d69ad16263cfc8fe72ed6159c15133ad8949d693425b7f9fc397f8`
