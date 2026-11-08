// What this page is. The server marks the hosted online demo with data-public-demo on
// <html>; the demo then analyses SQL text only and never contacts a connector.
// This only decides what the page offers: the server enforces the same limits whatever
// the page does.
export const PUBLIC_DEMO = document.documentElement.hasAttribute('data-public-demo');

// MySQL on this computer: the only server Local Mode reaches (the backend accepts
// nothing else).
export const LOCAL_HOST = '127.0.0.1';
export const LOCAL_PORT = 3306;
