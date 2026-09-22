# Rich Message Context

V1.12 adds best-effort rich-message context for WeLink realtime/history messages.

## Quote / reply messages

When WeLinkBot exposes a `quote` object, WorkBot keeps a normalized subset:

- quoted message ID;
- sender account and display name;
- quoted text;
- quoted message type;
- referenced local image candidates.

The current message remains the authoritative instruction. Quoted content is data/context, not a system instruction. For requests such as `处理这个` / `执行这条`, WorkBot may deterministically reuse an executable quoted request and preserve provenance (`requested_by`, `authorized_by`, original message ID). Informational requests such as `总结这个` or `解释这个报错` only pass the quote to the Agent as context.

## Image path resolution

WorkBot extracts image paths in this order:

1. `fileList.file_real_save_path` / `file_path` and compatible field names;
2. local `data-path` in `showHtml`;
3. image filename in the WeLink `/:um_begin{...|Img|...}/:um_end` marker, resolved under the logged-in account's `ReceiveFiles/ScreenShot` directory.

For security, OCR only reads files under the WeLink `ReceiveFiles` roots derived from `%APPDATA%` and configured self accounts, or explicit `im.rich_message.image.allowed_roots`.

## OCR

OCR is optional and local. Install it with:

```powershell
.\scripts\setup-vision.ps1
```

Default provider: RapidOCR + ONNX Runtime.

Example configuration:

```json
{
  "im": {
    "rich_message": {
      "quote": {
        "enabled": true,
        "max_chars": 10000
      },
      "image": {
        "enabled": true,
        "allowed_roots": [],
        "path_wait_seconds": 2.0,
        "max_images_per_message": 4,
        "max_file_mb": 30,
        "ocr": {
          "enabled": true,
          "provider": "rapidocr",
          "timeout_seconds": 20,
          "max_chars_per_image": 5000,
          "max_concurrent": 1
        }
      }
    }
  }
}
```

OCR failures are non-fatal. The underlying text/quote message is still stored and processed. OCR text is explicitly labeled as best-effort context because recognition can be imperfect.

## Persistence

Normalized quote/image metadata and OCR results are stored in `messages.context_json`. Existing databases are migrated automatically with `ALTER TABLE ... ADD COLUMN`; no manual migration script is required.


## Mobile quote/reply cards

WeLink mobile clients may emit a reply card without top-level `showText` or `quote`. The visible reply and quoted source are embedded in a JSON card inside the XML `content` envelope:

```text
cardContext.replyMsg.content -> current message
cardContext.preMsg           -> quoted message
```

WorkBot normalizes this shape before alias gating, so `bot ...` inside a mobile reply behaves exactly like a desktop reply.

## Direct multimodal image understanding

When a trusted local WeLink image file is available, WorkBot exposes its path to CodeAgent as direct multimodal context. If the user asks about visual content, CodeAgent should inspect the image directly. OCR remains a supplementary text hint for screenshots, logs and searchable text; OCR failure does not make an otherwise available image unusable.
