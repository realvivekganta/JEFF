import { fileURLToPath } from 'node:url';
import { defineConfig, loadEnv } from 'vite';

const frontendRoot = fileURLToPath(new URL('.', import.meta.url));
const backendRoot = fileURLToPath(new URL('../backend/', import.meta.url));

export default defineConfig(({ mode }) => {
  // Read only the backend port for the local API proxy. Secrets are not exposed.
  const env = loadEnv(mode, backendRoot, 'PORT');
  return {
    root: frontendRoot,
    build: { outDir: 'dist', emptyOutDir: true },
    server: {
      host: '127.0.0.1', port: 5173, strictPort: true,
      proxy: { '/api': { target: `http://127.0.0.1:${env.PORT || 3100}`, changeOrigin: true } },
    },
  };
});
