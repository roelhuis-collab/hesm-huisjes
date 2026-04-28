/**
 * POST /chat with SSE streaming.
 *
 * The Cloud Run /chat endpoint returns ``text/event-stream`` framed as
 * ``data: {...}\n\n``. Browsers' built-in ``EventSource`` is GET-only,
 * so we use ``fetch`` + ``ReadableStream`` and parse frames by hand.
 *
 * Frame schema (from src/ai/claude.py):
 *   * ``{"type": "delta", "text": "..."}``  — incremental token
 *   * ``{"type": "done"}``                  — stream complete
 */

import type { User } from 'firebase/auth';
import { API_BASE_URL } from './firebase';

export interface ChatMessage {
  role: 'user' | 'assistant';
  content: string;
}

export interface DeltaEvent {
  type: 'delta';
  text: string;
}

export interface DoneEvent {
  type: 'done';
}

export type StreamEvent = DeltaEvent | DoneEvent;

export async function* streamChat(
  user: User,
  messages: ChatMessage[],
  signal?: AbortSignal,
): AsyncGenerator<StreamEvent, void, void> {
  const token = await user.getIdToken();
  const res = await fetch(`${API_BASE_URL}/chat`, {
    method: 'POST',
    headers: {
      'content-type': 'application/json',
      authorization: `Bearer ${token}`,
    },
    body: JSON.stringify({ messages }),
    signal,
  });

  if (!res.ok || !res.body) {
    const body = res.body ? await res.text() : '';
    throw new Error(`chat ${res.status}: ${body.slice(0, 200)}`);
  }

  const reader = res.body.getReader();
  const decoder = new TextDecoder('utf-8');
  let buffer = '';

  try {
    while (true) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });

      // SSE frames are separated by a blank line. Process every complete frame.
      let sep: number;
      while ((sep = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);

        // A frame is one or more lines; we only care about ``data:`` lines.
        for (const line of frame.split('\n')) {
          if (line.startsWith('data: ')) {
            const payload = line.slice(6).trim();
            if (!payload) continue;
            try {
              yield JSON.parse(payload) as StreamEvent;
            } catch {
              // Bad frame — drop it, don't kill the stream.
            }
          }
        }
      }
    }
  } finally {
    reader.releaseLock();
  }
}
