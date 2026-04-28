/**
 * Firebase web SDK initialization.
 *
 * Auth + Firestore are exposed as singletons so any component or hook can
 * import them directly. The config comes from Vite env vars at build
 * time — values are public (Firebase web keys are designed to be), so we
 * embed them in the bundle and rely on Firestore rules + auth-domain
 * allowlist for actual security.
 */

import { initializeApp, type FirebaseOptions } from 'firebase/app';
import {
  getAuth,
  GoogleAuthProvider,
} from 'firebase/auth';
import { getFirestore } from 'firebase/firestore';

const firebaseConfig: FirebaseOptions = {
  apiKey: import.meta.env.VITE_FIREBASE_API_KEY,
  authDomain: import.meta.env.VITE_FIREBASE_AUTH_DOMAIN,
  projectId: import.meta.env.VITE_FIREBASE_PROJECT_ID,
  storageBucket: import.meta.env.VITE_FIREBASE_STORAGE_BUCKET,
  messagingSenderId: import.meta.env.VITE_FIREBASE_MESSAGING_SENDER_ID,
  appId: import.meta.env.VITE_FIREBASE_APP_ID,
};

if (!firebaseConfig.apiKey) {
  // Fail loud — better than silently rendering a broken sign-in.
  throw new Error(
    'VITE_FIREBASE_API_KEY missing. Copy .env.example to .env.local and fill it in.',
  );
}

export const firebaseApp = initializeApp(firebaseConfig);
export const auth = getAuth(firebaseApp);
export const db = getFirestore(firebaseApp);
export const googleProvider = new GoogleAuthProvider();

/** Cloud Run base URL — used for /chat and /override fetches. */
export const API_BASE_URL: string =
  import.meta.env.VITE_API_BASE_URL ??
  'https://hesm-optimizer-4rsk5dywaa-ez.a.run.app';
