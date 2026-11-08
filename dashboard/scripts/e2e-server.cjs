// Exercise the same socket-peer authentication boundary as the local app.
// Bare `next dev` does not attach the peer proof, so even loopback API calls fail.
process.env.PORT ||= '3001';
process.env.NERYA_PERMISSION_MODE ||= 'yolo';
process.env.NEXT_PUBLIC_NERYA_PERMISSION_MODE ||= process.env.NERYA_PERMISSION_MODE;
process.env.NERYA_E2E = '1';
require('./local-server.cjs').start().catch(error => { console.error(error); process.exitCode = 1; });
