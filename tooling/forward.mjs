import tls from 'node:tls';
import fs from 'node:fs';
import path from 'node:path';
// The library otherwise honors USBMUXD_SOCKET_ADDRESS, including remote TCP hosts.
// Refuse overrides before importing it, reading TLS material, or opening a listener.
if (process.env.USBMUXD_SOCKET_ADDRESS) throw new Error('USBMUXD_SOCKET_ADDRESS overrides are forbidden.');

const [udid, localArgument = '18100', deviceArgument = '8100'] = process.argv.slice(2);
if (!/^[A-Za-z0-9-]{8,64}$/.test(udid ?? '')) throw new Error('Provide a valid device UDID.');
function port(value) {
  if (!/^\d+$/.test(value)) throw new Error('Ports must be integers.');
  const number = Number(value);
  if (!Number.isInteger(number) || number < 1024 || number > 65535) throw new Error('Ports must be 1024-65535.');
  return number;
}
const localPort = port(localArgument);
const devicePort = port(deviceArgument);

function tlsMaterial() {
  const raw = process.env.IPHONE_USE_TLS_DIR;
  if (typeof raw !== 'string' || !path.isAbsolute(raw)) throw new Error('IPHONE_USE_TLS_DIR must be an absolute directory.');
  const link = fs.lstatSync(raw);
  if (!link.isDirectory() || link.isSymbolicLink()) throw new Error('IPHONE_USE_TLS_DIR must not be a symlink.');
  const real = fs.realpathSync(raw);
  const directory = fs.lstatSync(real);
  if (!directory.isDirectory() || directory.isSymbolicLink() || (directory.mode & 0o777) !== 0o700) {
    throw new Error('TLS directory mode must be 0700.');
  }
  const names = ['ca.pem', 'server.pem', 'server.key', 'client.pem', 'client.key'];
  const files = {};
  for (const name of names) {
    const file = path.join(real, name);
    const info = fs.lstatSync(file);
    if (!info.isFile() || info.isSymbolicLink() || info.nlink !== 1 || (info.mode & 0o077) !== 0) {
      throw new Error('TLS material must be one private regular file.');
    }
    files[name] = fs.readFileSync(file);
  }
  return files;
}
const material = tlsMaterial();
let deviceLibrary;
function devices() {
  deviceLibrary ??= import('appium-ios-device');
  return deviceLibrary;
}
function accepted(client) {
  if (!client.authorized) return false;
  const usage = client.getPeerCertificate()?.ext_key_usage;
  const values = Array.isArray(usage) ? usage : usage ? [usage] : [];
  return values.includes('TLS Web Client Authentication') || values.includes('1.3.6.1.5.5.7.3.2');
}

const sockets = new Set();
let stopping = false;
const server = tls.createServer({
  key: material['server.key'],
  cert: material['server.pem'],
  ca: material['ca.pem'],
  requestCert: true,
  rejectUnauthorized: true,
  minVersion: 'TLSv1.3',
  handshakeTimeout: 5000,
}, async client => {
  if (!accepted(client)) { client.destroy(); console.error('TLS client rejected'); return; }
  client.setNoDelay(true);
  sockets.add(client);
  client.on('error', () => client.destroy());
  client.on('close', () => sockets.delete(client));
  try {
    const {default: iosDevice} = await devices();
    if (stopping || client.destroyed) return;
    let timedOut = false;
    const pending = iosDevice.utilities.connectPort(udid, devicePort);
    const remote = await new Promise((resolve, reject) => {
      const timer = setTimeout(() => { timedOut = true; reject(new Error('timed out')); }, 8000);
      pending.then(value => {
        clearTimeout(timer);
        if (timedOut) value.destroy?.();
        else resolve(value);
      }, error => {
        clearTimeout(timer);
        if (!timedOut) reject(error);
      });
    });
    if (stopping || client.destroyed) { remote.destroy(); return; }
    remote.setNoDelay?.(true);
    sockets.add(remote);
    remote.on('error', error => { console.error(`device socket: ${error.message}`); client.destroy(); });
    remote.on('close', () => { sockets.delete(remote); client.destroy(); });
    client.on('close', () => remote.destroy());
    client.pipe(remote).pipe(client);
  } catch (error) {
    console.error(`USB connect failed: ${error.message}`);
    client.destroy();
  }
});
server.on('tlsClientError', (_error, socket) => { socket?.destroy(); console.error('TLS client rejected'); });
server.on('error', error => { console.error(`USB forward failed: ${error.message}`); process.exitCode = 1; stop(); });
server.listen(localPort, '127.0.0.1', () => console.log(`WDA USB forward listening at 127.0.0.1:${localPort}`));
function stop() {
  if (stopping) return;
  stopping = true;
  for (const socket of sockets) socket.destroy();
  server.close(() => process.exit(process.exitCode ?? 0));
  setTimeout(() => process.exit(process.exitCode ?? 0), 1000).unref();
}
process.on('SIGINT', stop);
process.on('SIGTERM', stop);
