import { defineConfig, loadEnv } from 'vite';
import react from '@vitejs/plugin-react';

export default defineConfig(({ mode }) => {
  const env = loadEnv(mode, process.cwd(), '');
  return {
    plugins: [react()],
    server: { proxy: { '/admin-api': { target: env.REGISTRY_API_TARGET || 'http://127.0.0.1:8002', rewrite: path => path.replace(/^\/admin-api/, '/admin') }, '/ui': {
      target: env.UI_API_TARGET || 'http://127.0.0.1:8001',
      // Server-side local-demo grant; never embed credentials in the JS bundle.
      headers: { Authorization: `Bearer ${env.UI_API_TOKEN || 'local-ui-demo'}` },
    } } },
    test: { environment: 'jsdom', setupFiles: ['./src/test/setup.ts'], css: false },
  };
});
