/**
 * Install hint for iOS Safari.
 *
 * On Android / desktop Chrome, the browser shows a native install prompt
 * via ``beforeinstallprompt`` — we just show a button that triggers it.
 *
 * On iOS Safari there is no programmatic install: the user must use the
 * Share menu → "Voeg toe aan beginscherm". So we render a small banner
 * once per device with a how-to. Dismissed state is stored in
 * localStorage; re-prompts are off forever once the user closes it.
 */

import { Share, X } from 'lucide-react';
import { useEffect, useState } from 'react';

const STORAGE_KEY = 'hesm:install-hint-dismissed';

interface NativePrompt extends Event {
  prompt: () => void;
  userChoice: Promise<{ outcome: 'accepted' | 'dismissed' }>;
}

function isStandalone(): boolean {
  // iOS Safari: navigator.standalone. Modern browsers: matchMedia.
  const navStandalone = (window.navigator as Navigator & { standalone?: boolean }).standalone;
  return !!navStandalone || window.matchMedia('(display-mode: standalone)').matches;
}

function isIos(): boolean {
  return /iPad|iPhone|iPod/.test(window.navigator.userAgent);
}

export function InstallHint() {
  const [dismissed, setDismissed] = useState(() => localStorage.getItem(STORAGE_KEY) === '1');
  const [nativePrompt, setNativePrompt] = useState<NativePrompt | null>(null);

  useEffect(() => {
    function onBefore(e: Event) {
      e.preventDefault();
      setNativePrompt(e as NativePrompt);
    }
    window.addEventListener('beforeinstallprompt', onBefore);
    return () => window.removeEventListener('beforeinstallprompt', onBefore);
  }, []);

  function dismiss() {
    localStorage.setItem(STORAGE_KEY, '1');
    setDismissed(true);
  }

  if (dismissed || isStandalone()) return null;

  // Native (Chrome/Edge) install — one-tap.
  if (nativePrompt) {
    return (
      <Banner onClose={dismiss}>
        <span>Installeer HESM op dit apparaat?</span>
        <button
          onClick={() => {
            nativePrompt.prompt();
            void nativePrompt.userChoice.then(() => dismiss());
          }}
          className="rounded-md bg-amber-400 px-3 py-1 text-xs font-medium text-slate-950"
        >
          Installeren
        </button>
      </Banner>
    );
  }

  // iOS — explain the Share-menu route.
  if (isIos()) {
    return (
      <Banner onClose={dismiss}>
        <span className="flex items-center gap-2">
          <Share size={14} className="text-amber-400" />
          Tik <strong>Delen</strong> in Safari → <strong>Voeg toe aan beginscherm</strong>.
        </span>
      </Banner>
    );
  }

  // Other browsers — just hide.
  return null;
}

function Banner({
  children,
  onClose,
}: {
  children: React.ReactNode;
  onClose: () => void;
}) {
  return (
    <div className="fixed inset-x-0 bottom-4 z-50 mx-auto flex max-w-md items-center justify-between gap-3 rounded-xl border border-slate-800 bg-slate-900/95 px-4 py-3 text-sm text-slate-200 shadow-lg backdrop-blur">
      <div className="flex flex-1 items-center gap-3">{children}</div>
      <button onClick={onClose} aria-label="sluit" className="text-slate-500 hover:text-slate-200">
        <X size={14} />
      </button>
    </div>
  );
}
