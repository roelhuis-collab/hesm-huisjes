/**
 * AI chat panel — POSTs to /chat and streams the reply token-by-token.
 *
 * Conversation history is kept in component state for the session;
 * refreshing the page wipes it. Persistence (per-user chat thread in
 * Firestore) can come later if it earns its keep.
 */

import { Loader2, Send, Sparkles, User as UserIcon } from 'lucide-react';
import { useEffect, useRef, useState } from 'react';
import { useAuth } from '../contexts/AuthContext';
import { type ChatMessage, streamChat } from '../lib/sseChat';

const SUGGESTIONS = [
  'Wat doet het systeem nu?',
  'Waarom is de dompelaar aan?',
  'Hoeveel zou ik vandaag besparen?',
];

export function ChatPanel() {
  const { user } = useAuth();
  const [messages, setMessages] = useState<ChatMessage[]>([]);
  const [draft, setDraft] = useState('');
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const abortRef = useRef<AbortController | null>(null);
  const scrollRef = useRef<HTMLDivElement | null>(null);

  useEffect(() => {
    scrollRef.current?.scrollTo({
      top: scrollRef.current.scrollHeight,
      behavior: 'smooth',
    });
  }, [messages]);

  async function send(text: string) {
    if (!user || streaming || !text.trim()) return;
    setError(null);

    const userMsg: ChatMessage = { role: 'user', content: text.trim() };
    const baseHistory = [...messages, userMsg];
    setMessages([...baseHistory, { role: 'assistant', content: '' }]);
    setDraft('');
    setStreaming(true);

    const controller = new AbortController();
    abortRef.current = controller;
    let acc = '';

    try {
      for await (const ev of streamChat(user, baseHistory, controller.signal)) {
        if (ev.type === 'delta') {
          acc += ev.text;
          setMessages([...baseHistory, { role: 'assistant', content: acc }]);
        }
        // 'done' event ends the stream naturally.
      }
    } catch (e) {
      if ((e as Error).name !== 'AbortError') {
        setError(String(e));
      }
    } finally {
      setStreaming(false);
      abortRef.current = null;
    }
  }

  function cancel() {
    abortRef.current?.abort();
  }

  return (
    <section className="flex h-[28rem] flex-col rounded-xl border border-slate-800 bg-slate-900/50">
      <header className="flex items-center justify-between border-b border-slate-800 px-5 py-3">
        <h2 className="flex items-center gap-2 text-[10px] uppercase tracking-[0.25em] text-slate-500">
          <Sparkles size={12} className="text-amber-400" /> Vraag de AI
        </h2>
        {streaming && (
          <button
            onClick={cancel}
            className="text-[10px] uppercase tracking-widest text-slate-500 hover:text-rose-300"
          >
            stop
          </button>
        )}
      </header>

      <div ref={scrollRef} className="flex-1 overflow-y-auto px-5 py-4">
        {messages.length === 0 ? (
          <div className="space-y-3">
            <p className="text-sm text-slate-400">
              Stel een vraag over wat het systeem doet, of kies een voorzet:
            </p>
            <div className="flex flex-wrap gap-2">
              {SUGGESTIONS.map((s) => (
                <button
                  key={s}
                  onClick={() => send(s)}
                  className="rounded-full border border-slate-800 bg-slate-900 px-3 py-1.5 text-xs text-slate-300 hover:border-amber-400/40 hover:bg-slate-800"
                >
                  {s}
                </button>
              ))}
            </div>
          </div>
        ) : (
          <ul className="space-y-4">
            {messages.map((m, i) => (
              <li key={i} className="flex items-start gap-3">
                <span
                  className={`mt-1 flex h-6 w-6 shrink-0 items-center justify-center rounded-full ${
                    m.role === 'user' ? 'bg-slate-800' : 'bg-amber-400/20'
                  }`}
                >
                  {m.role === 'user' ? (
                    <UserIcon size={12} className="text-slate-300" />
                  ) : (
                    <Sparkles size={12} className="text-amber-400" />
                  )}
                </span>
                <div className="flex-1 whitespace-pre-wrap text-sm text-slate-200">
                  {m.content || (
                    <span className="inline-flex items-center gap-2 text-slate-500">
                      <Loader2 size={12} className="animate-spin" /> denkt na…
                    </span>
                  )}
                </div>
              </li>
            ))}
          </ul>
        )}
      </div>

      {error && (
        <div className="border-t border-rose-900/40 bg-rose-950/20 px-5 py-2 text-xs text-rose-300">
          {error}
        </div>
      )}

      <form
        onSubmit={(e) => {
          e.preventDefault();
          send(draft);
        }}
        className="flex items-center gap-2 border-t border-slate-800 px-3 py-3"
      >
        <input
          type="text"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder="Stel een vraag…"
          disabled={streaming}
          className="flex-1 rounded-md bg-slate-900 px-3 py-2 text-sm placeholder-slate-600 outline-none focus:ring-1 focus:ring-amber-400/40 disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={!draft.trim() || streaming}
          className="rounded-md bg-amber-400 px-3 py-2 text-slate-950 disabled:cursor-not-allowed disabled:bg-slate-800 disabled:text-slate-500"
        >
          <Send size={14} />
        </button>
      </form>
    </section>
  );
}
