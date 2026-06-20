import pluginVue from 'eslint-plugin-vue'

export default [
  { ignores: ['dist/**', 'src/api/schema.d.ts', 'node_modules/**'] },
  ...pluginVue.configs['flat/essential'],
]
