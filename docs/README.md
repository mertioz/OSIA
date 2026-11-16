# OSIA technical guides

Updated on **16 November 2026** against the implementation and the
[OSIA Work Drive](https://drive.google.com/drive/folders/1dCXO0R_JyYnzQoVS59cAl9P5MTPn_kBz).
This directory contains the maintained technical guides and their index.

## Guides

| Topic | Guide |
| --- | --- |
| Public Thinking, floating capsule and activity ownership | [Thinking](thinking.md) |
| Tool schemas, dispatch, outcomes and chat commands | [Tools](tools.md) |
| Exact model reasoning capabilities and settings | [Reasoning](reasoning-capabilities.md) |
| DeepSeek's selectable model and transport | [DeepSeek](deepseek.md) |
| Claude native Messages and authentication | [Claude](claude.md) |
| Provider discovery and availability | [Providers](providers.md) |
| xAI API and provider-specific state | [Grok](grok.md) |
| Product versions, native build IDs and update ordering | [Versions](version-policy.md) |
| Windows packaging and installation | [Windows installer](windows-installer.md) |
| PC–Mobile LAN, WebRTC and relay connections | [Direct-first transport](webrtc-direct.md) |

## Implementation and project decisions

[AGENTS.md](../AGENTS.md) and the operator's current instructions govern work in
the repository. The [Drive rules index](https://drive.google.com/file/d/1uJqAH2Ch7g5Wc3DOav4FcEaGTEiIR10r/view)
identifies current product decisions. The guides describe implementation
contracts and link directly to their code.

For Windows/Linux builds, signing and publication, use the
[installer rules on Drive](https://drive.google.com/file/d/11nRvwHVxn0JzaZbKqE8Mzy6pEyVVBGK4/view)
with [Windows build tooling](../packaging/windows/build_windows.py) and the
[Linux packaging guide](../packaging/linux/README.md).

[osia/version.py](../osia/version.py) owns the Desktop source version;
[osia/release_notes.py](../osia/release_notes.py) owns embedded release notes.
The exact installed version comes from its native package/build metadata.
Published artifacts and completed acceptance checks are recorded in the
release manifests and the [daily Drive journal](https://drive.google.com/drive/folders/1ieE7Qf-v3LDzWq0cTcpOydJWL6QAcQRH).
