import React from 'react';
import ReactDOM from 'react-dom/client';
import Access, {ErrorBoundary} from './Access';
import {initializeTheme, ThemeProvider} from './Theme';
import './style.css';
import './extensions.css';
import './theme.css';
import './motion.css';
import {installSpotlight} from './Motion';

initializeTheme();
installSpotlight();
ReactDOM.createRoot(document.getElementById('root')!).render(<React.StrictMode><ThemeProvider><ErrorBoundary><Access /></ErrorBoundary></ThemeProvider></React.StrictMode>);
