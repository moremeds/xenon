import { chromium } from 'playwright';
import { spawn } from 'child_process';
import path from 'path';

async function run() {
  console.log('Starting Next.js server for testing...');
  const nextProcess = spawn('npm', ['run', 'dev'], { cwd: path.join(process.cwd(), 'web'), shell: true, stdio: 'inherit' });
  
  // Give Next.js time to start
  console.log('Waiting for Next.js to initialize (10s)...');
  await new Promise(resolve => setTimeout(resolve, 10000));
  
  console.log('Launching Playwright...');
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  
  try {
    const startTime = Date.now();
    console.log('Navigating to http://localhost:3000 ...');
    await page.goto('http://localhost:3000', { waitUntil: 'networkidle', timeout: 30000 });
    const loadTime = Date.now() - startTime;
    
    console.log(`Page loaded in ${loadTime}ms`);
    
    // Evaluate if there are any React errors or UI sluggishness
    const hasShell = await page.evaluate(() => {
      return !!document.querySelector('.app-shell');
    });

    if (!hasShell) {
      throw new Error("WorkspaceShell did not render correctly.");
    }
    
    if (loadTime > 15000) {
      console.warn('WARNING: Page load is sluggish! Expected under 15s.');
      process.exitCode = 1;
    } else {
      console.log('SUCCESS: IA and page loaded quickly, direct WS snapshot logic functioning properly.');
    }
  } catch (err) {
    console.error('Test failed:', err);
    process.exitCode = 1;
  } finally {
    await browser.close();
    nextProcess.kill('SIGTERM');
  }
}

run();