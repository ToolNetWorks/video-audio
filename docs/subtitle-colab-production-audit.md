# Subtitle Pipeline Production Readiness Audit

## P0: Security & Concurrency

1. **Config Injection Vulnerability**
   - **Finding:** VPS injected `runner.py` via plain string formatting (`f"CONFIG_JSON = '''{json.dumps(config)}'''"`). This allows trivial quote/newline injection.
   - **Fix:** Switched to base64 encoding `json.dumps(config)` and decoding it natively in the Colab python script.
   - **Status:** PASS

2. **Cancel State Concurrency Bug (Race Condition / Multi-job kill)**
   - **Finding:** Cancelling a job invoked `colab restart-kernel -s subtitle`. This killed ALL concurrently running jobs in that session. It also caused the background thread waiting on `colab exec` to throw an exception and incorrectly transition the cancelled job's state to a generic `failed`.
   - **Fix:** Implemented graceful cancellation. The VPS now drops a `cancel` file (`/content/loop-video-audio/{job_id}/cancel`). The Colab runner periodically checks for this file and safely exits without disturbing other jobs in the Jupyter kernel queue. Exception blocks were updated to preserve `cancelled` states.
   - **Status:** PASS

## P1: Validation & Deduplication

3. **SRT Chunk Boundary Duplication**
   - **Finding:** Overlap deduplication relied on simplistic text matching (`if text in prev["text"]`). This is fragile and often resulted in dropped or duplicated context near the boundary due to model variance.
   - **Fix:** Rewrote `merge_chunks_and_create_srt` to use a strict timestamp-aware deduplication. Overlaps > 40% are dropped, and segment start times are safely adjusted to guarantee monotonic, non-overlapping subtitle outputs.
   - **Status:** PASS

4. **SRT Content Validation**
   - **Finding:** The system previously trusted any non-empty downloaded file as a success, which could lead to malformed SRTs going to production.
   - **Fix:** Added `validate_srt(srt_content, expected_duration)`. Every generated SRT is rigorously validated (monotonic timestamps, strict formatting, sequence ID parity, duration sanity checks) before a job is marked successful.
   - **Status:** PASS

## P2: Resource & Cleanup

5. **Colab VM Disk Accumulation**
   - **Finding:** `colab exec` left raw media, chunk chunks, and merge artifacts indefinitely in `/content/loop-video-audio/{job_id}`.
   - **Fix:** `runner.py` now implements a `shutil.rmtree()` cleanup step for all large temporary inputs (`input_audio.media`, `results/`) immediately after `subtitle.srt` generation.
   - **Status:** PASS

6. **Token Leakage and Revocation**
   - **Finding:** Verified that `audio_url` temporary tokens are properly hashed, never directly printed to the `subtitle.log`, and successfully revoked on job success, failure, or cancellation.
   - **Status:** PASS

7. **Google Drive Dependency**
   - **Finding:** `start_subtitle_job` incorrectly failed if Google Drive wasn't mounted, even though the new architecture utilizes direct token streams and doesn't write to Drive.
   - **Fix:** Removed the obsolete `auth_manager.check_status()` requirement.
   - **Status:** PASS

## Blockers (RESOLVED)

* **PUBLIC_BASE_URL HTTPS (Security Blocker):** Audio files and authentication tokens were previously transmitted from the VPS to Colab via HTTP, which is insecure.
   - **Fix:** Implemented an automated `cloudflared` HTTPS tunnel script (`scripts/start_secure_tunnel.sh`) and updated `services/subtitle_service.py` to dynamically prioritize the secure `trycloudflare.com` HTTPS URL for all API token links sent to Colab. 
   - **Status:** PASS (Data in transit is now encrypted)
