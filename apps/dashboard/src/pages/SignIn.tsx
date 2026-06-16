/**
 * SignIn — verbindingsscherm.
 *
 * Auth gebeurt anoniem-op-de-achtergrond (zie AuthContext). Deze pagina is
 * de loading-state voor de paar honderd ms tussen "PWA opent" en "Firebase
 * Auth heeft een anonymous user". Bij netwerkfout valt het scherm hier
 * stil met de Firebase-error in beeld.
 */

import { Sparkles } from 'lucide-react';
import { useAuth } from '../contexts/AuthContext';

export default function SignIn() {
  const { signInError, loading } = useAuth();

  return (
    <div className="flex min-h-screen flex-col items-center justify-center px-8">
      <div className="max-w-sm text-center">
        <Sparkles size={28} className="mx-auto mb-6 text-amber-400" />
        <h1 className="mb-2 text-3xl font-extralight tracking-tight">HESM</h1>
        <p className="mb-12 text-xs uppercase tracking-[0.3em] text-slate-500">
          home energy by Huisjes
        </p>

        <p className="text-sm text-slate-400">
          {loading ? 'Verbinden…' : signInError ? 'Verbinden mislukt' : 'Bezig'}
        </p>

        {signInError && (
          <p className="mt-6 text-xs text-rose-300 break-all">
            {signInError}
          </p>
        )}
      </div>
    </div>
  );
}
