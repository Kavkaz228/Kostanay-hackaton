import { request } from '@playwright/test';
import { readFileSync, writeFileSync } from 'node:fs';

export default async function setup() {
  const client = await request.newContext({baseURL: process.env.BASE_URL || 'http://web:8080'});
  const password = 'Isolated E2E password 2026!';
  let response = await client.post('/api/auth/login', {data: {username: 'admin', password}});
  if (!response.ok()) {
    const bootstrap = readFileSync(process.env.BOOTSTRAP_PASSWORD_FILE!, 'utf8').trim();
    response = await client.post('/api/auth/login', {data: {username: 'admin', password: bootstrap}});
    if (!response.ok()) throw new Error('Cannot authenticate isolated test administrator');
    const user = await response.json();
    const changed = await client.post('/api/auth/password', {headers: {'X-CSRF-Token': user.csrf}, data: {current_password: bootstrap, new_password: password}});
    if (!changed.ok()) throw new Error('Cannot set isolated test administrator password');
    response = await client.post('/api/auth/login', {data: {username: 'admin', password}});
  }
  const user = await response.json();
  writeFileSync('/tmp/allur-csrf', user.csrf);
  await client.storageState({path: '/tmp/allur-auth.json'});
  await client.dispose();
}
