/**
 * Firebase Auth context.
 *
 * Sign-in uses signInWithRedirect everywhere. Reden: popup-flow botst op
 * Cross-Origin-Opener-Policy in moderne Chrome (window.closed-call wordt
 * geblokkeerd) en iOS Safari blokkeert popups in standalone-modus sowieso.
 *
 * Redirect-errors van Firebase worden in een React-state hier opgevangen
 * EN ook door SignIn getoond, zodat een falende sign-in op de telefoon niet
 * stilletjes verdampt in een onzichtbare console.
 */

import {
  GoogleAuthProvider,
  type User,
  browserLocalPersistence,
  getRedirectResult,
  onAuthStateChanged,
  setPersistence,
  signInWithRedirect,
  signOut as fbSignOut,
} from 'firebase/auth';
import { createContext, useContext, useEffect, useMemo, useState } from 'react';
import type { ReactNode } from 'react';
import { auth } from '../lib/firebase';

interface AuthContextValue {
  user: User | null;
  loading: boolean;
  signInError: string | null;
  signIn: () => Promise<void>;
  signOut: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

function describeAuthError(err: unknown): string {
  if (err && typeof err === 'object') {
    const e = err as { code?: string; message?: string };
    if (e.code) return `${e.code}${e.message ? ': ' + e.message : ''}`;
    if (e.message) return e.message;
  }
  return String(err);
}

export function AuthProvider({ children }: { children: ReactNode }) {
  const [user, setUser] = useState<User | null>(null);
  const [loading, setLoading] = useState(true);
  const [signInError, setSignInError] = useState<string | null>(null);

  useEffect(() => {
    setPersistence(auth, browserLocalPersistence).catch((err) => {
      console.error('persistence setup failed:', err);
    });

    getRedirectResult(auth).catch((err) => {
      console.error('redirect sign-in failed:', err);
      setSignInError(describeAuthError(err));
    });

    return onAuthStateChanged(auth, (u) => {
      setUser(u);
      setLoading(false);
    });
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      loading,
      signInError,
      signIn: async () => {
        setSignInError(null);
        try {
          const provider = new GoogleAuthProvider();
          await signInWithRedirect(auth, provider);
        } catch (err) {
          setSignInError(describeAuthError(err));
          throw err;
        }
      },
      signOut: async () => {
        await fbSignOut(auth);
      },
    }),
    [user, loading, signInError],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const ctx = useContext(AuthContext);
  if (!ctx) {
    throw new Error('useAuth must be used inside <AuthProvider>');
  }
  return ctx;
}
