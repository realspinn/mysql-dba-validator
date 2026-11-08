// Base styles without the embedded Open Sans: index.css carries it as data: URLs,
// which the page's CSP (default-src 'self') refuses. The app uses the system fonts.
import '@cloudscape-design/global-styles/index-base.css';
import './styles.css';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { App } from './App';
import { applyThemeChoice, readThemeChoice, themeCloudscape } from './theme';

applyThemeChoice(readThemeChoice());
themeCloudscape();
createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>);
