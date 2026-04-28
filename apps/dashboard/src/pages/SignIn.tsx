/**
 * Sign-in page — Google popup. iOS PWAs sometimes block popups; if that
 * shows up in real use, swap the popup for a redirect (signInWithRedirect).
 */

import { Sparkles } from 'lucide-react';
import { useState } from 'react';
import { useAuth } from '../contexts/AuthContext';

export default function SignIn() {
  const { signIn } = useAuth();
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handleSignIn() {
    setBusy(true);
    setError(null);
    try {
      await signIn();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  return (
    <div className="flex min-h-screen flex-col items-center justify-center px-8">
      <div className="max-w-sm text-center">
        <Sparkles size={28} className="mx-auto mb-6 text-amber-400" />
        <h1 className="mb-2 text-3xl font-extralight tracking-tight">HESM</h1>
        <p className="mb-12 text-xs uppercase tracking-[0.3em] text-slate-500">
          home energy by Huisjes
        </p>

        <button
          onClick={handleSignIn}
          disabled={busy}
          className="
            w-full rounded-2xl border border-slate-800 bg-slate-900
            px-8 py-4 text-base font-light
            transition-all hover:border-amber-400/40 hover:bg-slate-800
            active:scale-[0.98]
            disabled:opacity-50
          "
        >
          {busy ? 'Bezig…' : 'Inloggen met Google'}
        </button>

        {error && (
          <p className="mt-6 text-xs text-rose-300">
            Inloggen mislukt: {error}
          </p>
        )}
      </div>
    </div>
  );
}
