// The workspace entry. Cloudscape's base styles without its embedded Open Sans: the full
// stylesheet carries the font as data: URLs, which the page's CSP refuses. The app uses
// the system fonts.
import '@cloudscape-design/global-styles/index-base.css';
import '../styles/workspace.css';
import { StrictMode } from 'react';
import { createRoot } from 'react-dom/client';
import { themeCloudscape } from '../shared/cloudscapeTheme';
import { PUBLIC_DEMO } from '../shared/environment';
import { applyThemeChoice, readThemeChoice } from '../shared/theme';
import { setDemoWording } from '../features/review/reviewText';
import { App } from './App';

applyThemeChoice(readThemeChoice());
themeCloudscape();
setDemoWording(PUBLIC_DEMO);
createRoot(document.getElementById('root')!).render(<StrictMode><App /></StrictMode>);
