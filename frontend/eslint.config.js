import js from '@eslint/js'
import globals from 'globals'
import reactHooks from 'eslint-plugin-react-hooks'
import reactRefresh from 'eslint-plugin-react-refresh'
import tseslint from 'typescript-eslint'
import { defineConfig, globalIgnores } from 'eslint/config'

export default defineConfig([
  globalIgnores(['dist', 'src/pages/_CatalogHome_backup/**']),
  {
    files: ['**/*.{ts,tsx}'],
    extends: [
      js.configs.recommended,
      tseslint.configs.recommended,
      reactHooks.configs.flat.recommended,
      reactRefresh.configs.vite,
    ],
    languageOptions: {
      ecmaVersion: 2020,
      globals: globals.browser,
    },
    rules: {
      // Rule too strict for our code: it flags the legitimate pattern setLoading(true) before
      // fetch.
      // There are no real cascading renders — most cases are ordinary loading-state initialization.
      'react-hooks/set-state-in-effect': 'off',
      // any should be visible but not block CI. Fix gradually (refactoring stage).
      '@typescript-eslint/no-explicit-any': 'warn',
      // Dev-only HMR warning. Fixed by moving hooks/sub-components into separate files (refactoring
      // stage).
      'react-refresh/only-export-components': 'warn',
    },
  },
])
