# Speech tab: transcribe a video or audio file (2026-11-20)

Andy asked for videos to be transcribed in the Speech tab, with the result
saved as Markdown or plain text.

## What the user does

1. Speech tab, box "Transcribe a video or audio file".
2. Pick the model (OpenAI Whisper only: large-v3-turbo by default, large-v3,
   medium, small, base, tiny), where it runs (GPU, else CPU / GPU only /
   CPU only), the language (or detect), Markdown or Text, Timestamps on/off.
3. "🎬 Transcribe a file…" and pick the file (mp4, mkv, mov, avi, webm, wav,
   mp3, m4a, …). The words appear as they are recognised, with a progress bar.
   Cancel stops it at once.
4. When it finishes the transcript is saved BESIDE THE FILE with the same
   name (`talk.mp4` -> `talk.md` / `talk.txt`). An existing file is never
   overwritten: it becomes `talk (2).md`. "Save as…" saves it anywhere else,
   in either format (by the extension chosen).

The first time a model is used it has to be downloaded: the note under the
options says so, and "Download model" fetches it once (the only step that
uses the internet, and only when pressed). After that it runs offline.
Models live in the normal Hugging Face cache (`%USERPROFILE%\.cache\huggingface`).

Record 5s / Transcribe / Send to Council (dictation) were ported in the same
change: Record needs sounddevice + soundfile; Send to Council puts the text in
the Council tab's question box.

## How it works (`council_core/transcribe.py`)

- The model runs in a CHILD process (`python -c …` or `Council.exe
  --transcribe-worker`), the job in the env var COUNCIL_TRANSCRIBE_JOB, events
  back on stdout as JSON lines behind `@@council-transcribe `.
- Why a child: on Windows CTranslate2 EXITS the process when a CUDA library
  (cublas64_12.dll, cudnn_ops64_9.dll) is missing. In a child that is one
  failed attempt; "GPU, else CPU" then runs the same job on the CPU and says
  so. A missing model is not retried on the CPU.
- GPU: float16; CPU: int8. Before a GPU run the child adds torch\lib,
  site-packages\nvidia\*\bin and %CUDA_PATH%\bin to the DLL search path, so a
  PC with CUDA PyTorch (Andy's desktop) finds cuBLAS / cuDNN there.
- Sound: PyAV (ships with faster-whisper) decodes the file to 16 kHz mono;
  if it cannot, the ffmpeg program on PATH does.
- `local_files_only=True` and HF_HUB_OFFLINE=1 in the child. The Download
  button runs its own child with HF_HUB_OFFLINE removed (the Connections tab
  sets it in the app's process).
- No voice-activity filter: faster-whisper's `vad_filter` uses Silero VAD,
  which is not US-made.

Tests: `tests/test_transcribe_file.py` (a fake child speaks the protocol; the
child's body runs against a fake faster_whisper).

## Not done / next

- Not yet run on Andy's RTX 5080 with a real model (the cloud test machine
  cannot download models). If the GPU run fails there, the status line shows
  the GPU's error and the CPU run continues.
- Speaker names (diarization) are not included: the usual models (pyannote)
  are not US-made.
