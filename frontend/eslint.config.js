import { defineConfigWithVueTs, vueTsConfigs } from '@vue/eslint-config-typescript'
import pluginVue from 'eslint-plugin-vue'

// Composes the Vue essential rules with the TypeScript parser/rules so that
// <script lang="ts"> blocks in SFCs are parsed as TypeScript (not espree, which
// chokes on `interface`/type annotations).
export default defineConfigWithVueTs(
  { ignores: ['dist/**', 'dist-guest/**', 'src/api/schema.d.ts', 'node_modules/**'] },
  pluginVue.configs['flat/essential'],
  vueTsConfigs.recommended,
)
