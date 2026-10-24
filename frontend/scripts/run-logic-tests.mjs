// Docker-only frontend logic suite. Local and Actions call the same entry.
import { readdirSync, existsSync } from 'node:fs'
import { basename } from 'node:path'
import { spawnSync } from 'node:child_process'

const esbuild = './node_modules/.bin/esbuild'
const prereqs = {
  'config_validation_mappers.test.mjs': [
    ['src/features/config/index.ts', '/tmp/b1-config.cjs'],
    ['src/features/validation/index.ts', '/tmp/b1-validation.cjs'],
  ],
  'help_markdown.test.mjs': [['src/lib/helpMarkdown.ts', '/tmp/help-md.cjs']],
  'modeling_draft_mapper.test.mjs': [['src/pages/model/canvasModel.ts', '/tmp/modeling-draft-mapper.cjs']],
  'modeling_mappers.test.mjs': [['src/features/modeling/mappers.ts', '/tmp/modeling-mappers.cjs']],
  'project_versions_mapper.test.mjs': [['src/features/project-versions/mappers.ts', '/tmp/project-versions-mappers.cjs']],
  'tasks_mappers.test.mjs': [['src/features/tasks/index.ts', '/tmp/b1-tasks.cjs']],
  'upload_head_scan.test.mjs': [['src/pages/uploadHeadScan.ts', '/tmp/upload-head-scan.cjs']],
}
function execute(command, args) {
  const result = spawnSync(command, args, { stdio: 'inherit', cwd: '/app' })
  if (result.error) throw result.error
  if (result.status !== 0) throw new Error(`${command} exited with ${result.status}`)
}
function bundle(entry, target, format) {
  execute(esbuild, [
    entry, '--bundle', '--platform=node', `--format=${format}`,
    '--define:import.meta.env.DEV=false',
    '--define:import.meta.env.PROD=true',
    `--outfile=${target}`,
  ])
}
const available = readdirSync('tests').filter(x => x.endsWith('.test.mjs')).sort()
const selected = process.argv.slice(2).length ? [...new Set(process.argv.slice(2))] : available
if (!selected.length) throw new Error('No frontend logic test selected')
for (const name of selected) {
  if (basename(name) !== name || !available.includes(name))
    throw new Error(`Invalid or nonexistent frontend test: ${name}`)
}
let passed = 0
const failed = []
for (const name of selected) {
  console.log(`\n=== Frontend logic: ${name} ===`)
  try {
    for (const [entry, outfile] of prereqs[name] || []) bundle(entry, outfile, 'cjs')
    if (prereqs[name]) {
      execute('node', [`tests/${name}`])
    } else {
      const target = `/tmp/${name}.mjs`
      bundle(`tests/${name}`, target, 'esm')
      execute('node', [target])
    }
    passed++
  } catch (error) {
    failed.push(name)
    console.error(`[FAIL] ${name}: ${error.message}`)
  }
}
console.log(`Frontend logic: ${passed}/${selected.length} files passed`)
if (failed.length) {
  console.error(`Failed files: ${failed.join(', ')}`)
  process.exitCode = 1
}
