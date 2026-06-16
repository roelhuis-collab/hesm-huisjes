/**
 * Firebase Auth context — anonymous-by-default.
 *
 * We zaten vast in de iOS-PWA-trap met Google sign-in: redirect-chain liep
 * door cross-origin proxies en de WebView verloor de auth-state. Voor een
 * persoonlijk huis-systeem op een onbekende .netlify.app-URL is dat
 * onevenredig veel pijn voor de waarde die login toevoegt.
 *
 * Oplossing zolang ``controllable=false`` blijft: anonymous Firebase Auth.
 * Klant opent de app, AuthProvider doet ``signInAnonymously`` op de
 * achtergrond, Firestore-rules (``request.auth != null``) blijven gerespecteerd,
 * geen popup, geen redirect, geen iOS-WebView-issue.
 *
 * Wanneer de engine straks fysiek gaat schakelen (controllable=true) zetten
 * we Google sign-in terug — dan willen we de identiteit echt weten — maar
 * via een server-side auth-check op Cloud Run, niet via een client-side
 * Firebase-popup in een iOS-PWA.
 */

import {
  type User,
  browserLocalPersistence,
  onAuthStateChanged,
  setPersistence,
  signInAnonymously,
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

    const unsub = onAuthStateChanged(auth, (u) => {
      if (u) {
        setUser(u);
        setLoading(false);
        return;
      }
      // Geen user → anoniem inloggen. ``signInAnonymously`` doet één call
      // zonder navigatie of postMessage en triggert onAuthStateChanged opnieuw
      // met de nieuwe user. Bij netwerkfout valt-ie terug op signInError-UI.
      signInAnonymously(auth).catch((err) => {
        console.error('anonymous sign-in failed:', err);
        setSignInError(describeAuthError(err));
        setLoading(false);
      });
    });

    return unsub;
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({
      user,
      loading,
      signInError,
      signIn: async () => {
        setSignInError(null);
        try {
          await signInAnonymously(auth);
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
