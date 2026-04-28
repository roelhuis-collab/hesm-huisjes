/**
 * Top-level routing + auth guard.
 *
 * - ``/signin``                  : public, the only route reachable signed-out
 * - ``/``                        : Simple page (the iPad default)
 * - ``/advanced``                : detail dashboard (placeholder until PR11c)
 * - ``/settings``                : settings landing
 * - ``/settings/limits``         : Layer-1 hard limits
 * - ``/settings/strategy``       : Layer-2 strategy + weights
 * - ``/settings/learning``       : Layer-3 activation
 * - ``/settings/connectors``     : /health wiring map
 *
 * Everything except ``/signin`` requires an authenticated user; the
 * ``RequireAuth`` wrapper handles the redirect.
 */

import {
  Navigate,
  Route,
  BrowserRouter as Router,
  Routes,
} from 'react-router-dom';
import type { ReactNode } from 'react';
import { useAuth } from './contexts/AuthContext';
import Advanced from './pages/Advanced';
import SignIn from './pages/SignIn';
import Simple from './pages/Simple';
import Connectors from './pages/settings/Connectors';
import Learning from './pages/settings/Learning';
import Limits from './pages/settings/Limits';
import SettingsIndex from './pages/settings';
import StrategyPage from './pages/settings/Strategy';

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
        <Route path="/" element={<RequireAuth><Simple /></RequireAuth>} />
        <Route path="/advanced" element={<RequireAuth><Advanced /></RequireAuth>} />
        <Route path="/settings" element={<RequireAuth><SettingsIndex /></RequireAuth>} />
        <Route path="/settings/limits" element={<RequireAuth><Limits /></RequireAuth>} />
        <Route path="/settings/strategy" element={<RequireAuth><StrategyPage /></RequireAuth>} />
        <Route path="/settings/learning" element={<RequireAuth><Learning /></RequireAuth>} />
        <Route path="/settings/connectors" element={<RequireAuth><Connectors /></RequireAuth>} />
        <Route path="*" element={<Navigate to="/" replace />} />
      </Routes>
    </Router>
  );
}
