# OSIA documentation

Reviewed on **16 November 2026**. This directory separates maintained technical
guides from dated validation records. A larger WOR number, a later file
modification time, or an old report's next-step list does not establish product
authority.

## Where current decisions live

The operator's current instructions and [AGENTS.md](../AGENTS.md) govern work in
this repository. The [OSIA Work Drive](https://drive.google.com/drive/folders/1dCXO0R_JyYnzQoVS59cAl9P5MTPn_kBz)
is the project decision and delivery record. Its [rules index](https://drive.google.com/file/d/1uJqAH2Ch7g5Wc3DOav4FcEaGTEiIR10r/view)
identifies the current decisions and their historical sources.

The guides below explain implementation contracts and link to executable
sources. They do not create a second set of agent permissions, planners,
language rules or publication gates. Historical measurements describe the
commit and environment in which they were obtained; they are not current
acceptance results.

## Maintained technical guides

| Topic | Document |
| --- | --- |
| Public Thinking panel and its lifecycle | [Thinking](WOR-340-thinking-v5.md) |
| Model-specific reasoning capabilities | [Reasoning](reasoning-capabilities.md) |
| DeepSeek's canonical model and transport | [DeepSeek](WOR-298-deepseek-flash.md) |
| Claude native Messages and authentication | [Claude](WOR-344-claude-authentication.md) |
| Provider discovery and availability | [Cloud providers](WOR-355-cloud-provider-audit.md) |
| xAI API and provider-specific state | [Grok](WOR-356-grok-api-audit.md) |
| Tool verification and evidence sources | [Tool verification](tool-e2e-matrix.md) |
| Product versions, build IDs and update ordering | [Version policy](version-policy.md) |
| Windows packaging and installation | [Windows distribution](windows-installer.md) |
| PC–Mobile direct connections and relay | [Direct-first transport](webrtc-direct.md) |

For Windows/Linux rebuilds, signing and publishing, use the
[installer rules on Drive](https://drive.google.com/file/d/11nRvwHVxn0JzaZbKqE8Mzy6pEyVVBGK4/view)
and the scripts of the exact release commit. The version/source declaration is
[osia/version.py](../osia/version.py); actual public versions and acceptance
results belong to the release manifests and the daily Drive journal.

## Dated findings and evidence

These reports retain distinct findings and measured results rather than
duplicating the maintained rules:

- [Public prompt audit](WOR-341-prompt-verification-audit.md).
- [Tool reliability audit](WOR-341-tool-reliability.md).
- [Settings performance measurements](WOR-352-settings-performance.md).
- [Historical version inventory](WOR-357-version-inventory.md).
- [Windows 0.9.0 candidate evidence](WOR-349-windows-0.9.0-rc.md).

Local, untracked capsule, WOR-285 and WOR-305 reports contain additional
experiment results. Their test profiles, snapshots, logs, fixture instructions,
third-party documents and media are evidence or test inputs, not project rules.
They are not promoted into the public repository by this consolidation.

## Superseded documents

Earlier Thinking versions are consolidated into the current Thinking guide.
Overlapping provider and Windows delivery instructions are consolidated into
the topical guides above. Unique dated evidence remains in the relevant
historical summaries. The original published documents can be inspected at the
immutable source revision below; these links are for provenance, not work
instructions.

| Former document | Current destination | Original record |
| --- | --- | --- |
| Thinking V2 | Thinking guide | [Original](https://github.com/mertioz/OSIA/blob/3c6c5beb2979de717a46ee8ccfc56235ad3eeba1/docs/WOR-340-thinking.md) |
| Thinking V3 | Thinking guide | [Original](https://github.com/mertioz/OSIA/blob/3c6c5beb2979de717a46ee8ccfc56235ad3eeba1/docs/WOR-340-thinking-v3.md) |
| Thinking V4 | Thinking guide | [Original](https://github.com/mertioz/OSIA/blob/3c6c5beb2979de717a46ee8ccfc56235ad3eeba1/docs/WOR-340-thinking-v4.md) |
| WOR-343 provider audit | Provider guides and their dated findings | [Original](https://github.com/mertioz/OSIA/blob/3c6c5beb2979de717a46ee8ccfc56235ad3eeba1/docs/WOR-343-provider-audit.md) |
| WOR-354/356 delivery recap | Provider validation history | [Original](https://github.com/mertioz/OSIA/blob/3c6c5beb2979de717a46ee8ccfc56235ad3eeba1/docs/WOR-354-356-delivery.md) |
| WOR-357 ChatGPT Windows portability | Windows distribution | [Original](https://github.com/mertioz/OSIA/blob/3c6c5beb2979de717a46ee8ccfc56235ad3eeba1/docs/WOR-357-chatgpt-windows-portability.md) |
| WOR-357 source delivery | Version policy and dated provider/Windows findings | [Original](https://github.com/mertioz/OSIA/blob/3c6c5beb2979de717a46ee8ccfc56235ad3eeba1/docs/WOR-357-source-delivery.md) |

The complete local documentation tree was backed up outside the checkout before
this cleanup. Captured hashes and binary/JSON/log evidence are preserved; an
old result is neither erased nor upgraded to a new success claim.
