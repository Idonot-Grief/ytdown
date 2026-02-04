# ytdown
A youtube download flask api server
```
http://ytdownapi.example.com/watch?v=dQw4w9WgXcQ&res=720&audio=192&token=githubguy-69420&not-json
```

prepare the libraries
```
pip install flask yt-dlp
```
or use this
```
python -m pip install flask yt-dlp
```

# YouTube Downloader API Documentation

## Overview

This API allows you to download YouTube videos in MP4 or WebM format with customizable quality settings. It supports both JSON and direct download modes.

## Rate Limiting

- **10 downloads per day** per IP address
- Rate limit resets at midnight UTC

---

## Endpoints

### 1. `/watch` - Initiate Download

Start a video download request.

#### Parameters

| Parameter | Type | Required | Default | Description |
|-----------|------|----------|---------|-------------|
| `v` | string | **Yes** | - | YouTube video ID (e.g., `eXdIDjzy6KY`) |
| `format` | string | No | `mp4` | Output format: `mp4` or `webm` |
| `res` | integer | No | - | Maximum video height in pixels (e.g., `720`, `1080`) |
| `audio` | integer | No | `192` | Audio bitrate in kbps (e.g., `128`, `192`, `256`) |
| `token` | string | No | auto-generated | Custom token for tracking (auto-generated if not provided) |
| `not-json` | flag | No | - | Enable direct download mode (see below) |

#### JSON Mode (Default)

Returns information about the queued download with endpoints to check progress and retrieve the file.

**Example Request:**
```
GET /watch?v=eXdIDjzy6KY&format=mp4&res=1080&audio=256
```

**Example Response:**
```json
{
  "status": "queued",
  "token": "98c7ef8912c140efafb20042875f0afc",
  "progress": "/progress?token=98c7ef8912c140efafb20042875f0afc",
  "download": "/download?token=98c7ef8912c140efafb20042875f0afc"
}
```

#### Not-JSON Mode

Add `not-json` parameter to receive the file directly once processing completes. The request will wait until the download finishes, then stream the file.

**Example Request:**
```
GET /watch?v=eXdIDjzy6KY&not-json
```

**Response:** File download (blocks until ready)

---

### 2. `/progress` - Check Download Progress

Monitor the status and progress of your download.

#### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `token` | string | **Yes** | Token received from `/watch` |

**Example Request:**
```
GET /progress?token=98c7ef8912c140efafb20042875f0afc
```

**Example Response:**
```json
{
  "token": "98c7ef8912c140efafb20042875f0afc",
  "status": "downloading",
  "percent": 44.2,
  "speed_bps": 47560704,
  "eta_seconds": 12,
  "downloaded_bytes": 19345920,
  "total_bytes": 43847680,
  "format": "mp4",
  "elapsed": 8.5,
  "ready": false,
  "error": null
}
```

#### Status Values

- `queued` - Download is waiting to start
- `downloading` - Currently downloading video
- `processing` - Merging video and audio streams
- `done` - File is ready for download
- `error` - Download failed (check `error` field)

---

### 3. `/download` - Retrieve File

Download the completed video file.

#### Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `token` | string | **Yes** | Token received from `/watch` |

**Example Request:**
```
GET /download?token=98c7ef8912c140efafb20042875f0afc
```

**Response:** File download (only works when `status` is `done`)

---

## Usage Examples

### Example 1: Basic Download (JSON Mode)

```bash
# Step 1: Start download
curl "http://localhost/watch?v=eXdIDjzy6KY"

# Response:
# {
#   "status": "queued",
#   "token": "abc123...",
#   "progress": "/progress?token=abc123...",
#   "download": "/download?token=abc123..."
# }

# Step 2: Check progress (poll until ready)
curl "http://localhost/progress?token=abc123..."

# Step 3: Download file when ready
curl -O "http://localhost/download?token=abc123..."
```

### Example 2: High Quality Download

```bash
# Download 1080p video with 256kbps audio in MP4
curl "http://localhost/watch?v=eXdIDjzy6KY&res=1080&audio=256&format=mp4"
```

### Example 3: Direct Download (Not-JSON Mode)

```bash
# Download directly without polling
curl -O "http://localhost/watch?v=eXdIDjzy6KY&not-json"
# This will wait until the download completes, then save the file
```

### Example 4: WebM Format

```bash
# Download as WebM instead of MP4
curl "http://localhost/watch?v=eXdIDjzy6KY&format=webm"
```

---

## Error Responses

### 400 Bad Request
```json
{"error": "Missing v"}          // No video ID provided
{"error": "Invalid format"}     // Format must be mp4 or webm
```

### 403 Forbidden
```json
{"error": "Forbidden"}          // Token belongs to different IP
```

### 404 Not Found
```json
{"error": "Invalid or expired token"}  // Token doesn't exist or expired
```

### 409 Conflict
```json
{"error": "Token in use"}              // Token is already being used
{"error": "Not ready or expired"}      // File not ready or token expired
```

### 429 Too Many Requests
```json
{"error": "Daily limit reached"}       // Exceeded 10 downloads per day
```

---

## Important Notes

### Token Expiration
- Tokens remain valid for **5 minutes** after download completes
- Files are automatically deleted **6 minutes** after download completes
- You must download the file within this window

### IP Restrictions
- Each token is tied to the requesting IP address
- You cannot download a file from a different IP than the one that requested it

### Concurrent Downloads
- Maximum of **4 parallel downloads** server-wide
- Additional requests are queued automatically

### Video ID Format
The `v` parameter should be the YouTube video ID, which is the part after `watch?v=` in YouTube URLs.

Example: `https://www.youtube.com/watch?v=eXdIDjzy6KY` → video ID is `eXdIDjzy6KY`
