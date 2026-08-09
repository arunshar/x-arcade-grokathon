FROM python:3.13-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# Default offline demo. The HF Space secrets panel can set ARCADE_MODE=live
# plus XAI_API_KEY so Imagine / voice run for real.
ENV ARCADE_MODE=demo
# Prefer prebaked decoy mp4s; when generating live, use the fast path
# (text brief + short T2V, no vision still) so rounds are not stuck on "loading".
ENV ARCADE_IMAGINE_FAST=1
ENV ARCADE_IMAGINE_VIDEO_DURATION=1
ENV ARCADE_IMAGINE_WARMUP=1
EXPOSE 7860
CMD ["uvicorn", "server.app:app", "--host", "0.0.0.0", "--port", "7860"]
