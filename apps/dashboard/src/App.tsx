/**
 * Top-level routing + auth guard.
 *
 * - ``/signin`` : public, the only route reachable signed-out
 * - everything else: requires an authenticated user, otherwise redirected
 *
 * PR11b adds /advanced and /settings/*; for now everything except /signin
 * goes to the Simple page.
 */

import { Navigate, Route, BrowserRouter as Router, Routes } from 'react-router-dom';
import type { ReactNode } from 'react';
import { useAuth } from './contexts/AuthContext';
import SignIn from './pages/SignIn';
import Simple from './pages/Simple';

function RequireAuth({ children }: { children: ReactNode }) {
  const { user, loading } = useAuth();
  if (loading) {
    return (
      <div className="flex min-h-screen items-center justify-center text-xs uppercase tracking-widest text-slate-600">
        laden…
      </div>
    );
  }
  if (!user) {
    return <Navigate to="/signin" replace />;
  }
  return <>{children}</>;
}

export default function App() {
  return (
    <Router>
      <Routes>
        <Route path="/signin" element={<SignIn />} />
        <Route
          path="/"
          element={
            <RequireAuth>
              <Simple />
            </RequireAuth>
          }
        />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Router>
  );
}
