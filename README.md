# OSIA Desktop

OSIA is a desktop AI assistant with a paired mobile client. The PC owns the
canonical Chat runtime, selected model, tool execution and conversation state.
The mobile app projects that state and supplies user controls.

## Source and downloads

- Desktop: https://github.com/mertioz/OSIA
- Mobile: https://github.com/mertioz/osia-mobile
- Worker: https://github.com/mertioz/osia-worker
- Official downloads and documentation: https://osia.space
- Support: https://osia.space/support

### Run OSIA on Windows

1. Open [GitHub Releases](https://github.com/mertioz/OSIA/releases/latest) and download the **OSIA-Windows-…-x64.zip** file under **Assets**.
2. Right-click the ZIP, choose **Extract All**, and select a folder.
3. Open the extracted `OSIA` folder and run `OSIA.exe`. Keep `_internal` beside it.

The Windows download page includes the ZIP checksum, shortcut instructions and
the optional installer. [English guide](https://osia.space/download/windows#installation) · [Guide français](https://osia.space/fr/download/windows#installation)

For Windows source setup, use install.bat, start.bat and requirements.txt.
For the existing Linux package workflow, see packaging/linux/README.md.
User configuration, provider credentials and runtime data are local and are not
part of the source distribution. Start with the supplied example configuration.

## Voice activation preview

In Settings → Wake word, enable **Activate by saying “Hé OSIA”** to try local,
offline wake detection. Say “Hé OSIA” (O-ssia, with an s), wait for the listening indication, then
speak your request. Ctrl+Alt+P always starts manual listening. Acoustic listening
is off by default, and microphone audio is not saved. Command transcription
uses your configured STT provider. See [model, training and acceptance details](assets/wake/MODEL_CARD.md).

## License

Original OSIA code, documentation, logo and visual assets use the
[MIT License](LICENSE), which permits commercial reuse. See [LICENSING.md](LICENSING.md)
and [third-party notices](THIRD_PARTY_NOTICES.md) for dependency terms.

En français, la licence MIT autorise la réutilisation commerciale du code et
des visuels originaux d’OSIA.

Report vulnerabilities privately as described in SECURITY.md.
