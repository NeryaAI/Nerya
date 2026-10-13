import fs from 'node:fs';
import path from 'node:path';
import crypto from 'node:crypto';
import {createRequire} from 'node:module';
import {fileURLToPath} from 'node:url';
const root = path.dirname(fileURLToPath(import.meta.url));
const modules = path.resolve(process.env.MONAD_ARTIFACT_NODE_MODULES || '/Users/rick/Documents/Project/Nerya/pitch-video/monad-metropolis-20261013/v3/fork/node_modules');
const require = createRequire(path.join(modules, 'solc', 'package.json'));
const sha = b => crypto.createHash('sha256').update(b).digest('hex');
const packages = {'@uniswap/v3-core':'1.0.1','@uniswap/v3-periphery':'1.4.4',solc:'0.8.28'};
const provenance = {};
for (const [pkg, version] of Object.entries(packages)) {
  const actual = JSON.parse(fs.readFileSync(path.join(modules, pkg, 'package.json'))).version;
  if (actual !== version) throw new Error('Artifact version mismatch: '+pkg+' '+actual);
  provenance[pkg] = {version, origin:'pinned npm package'};
}
const solc = require(path.join(modules,'solc'));
const source = fs.readFileSync(path.join(root,'TestAssets.sol'),'utf8');
const settings = {optimizer:{enabled:true,runs:200},evmVersion:'paris',outputSelection:{'*':{'*':['abi','evm.bytecode.object','evm.deployedBytecode.object']}}};
const output = JSON.parse(solc.compile(JSON.stringify({language:'Solidity',sources:{'TestAssets.sol':{content:source}},settings})));
for (const e of output.errors || []) { if (e.severity === 'error') throw new Error(e.formattedMessage); }
const artifacts = {};
for (const [name,a] of Object.entries(output.contracts['TestAssets.sol'])) {
  artifacts[name] = {abi:a.abi,bytecode:'0x'+a.evm.bytecode.object,deployedBytecode:'0x'+a.evm.deployedBytecode.object,origin:'test-only source',source_sha256:sha(source)};
}
for (const [name,rel] of Object.entries({
  UniswapV3Factory:'@uniswap/v3-core/artifacts/contracts/UniswapV3Factory.sol/UniswapV3Factory.json',
  UniswapV3Pool:'@uniswap/v3-core/artifacts/contracts/UniswapV3Pool.sol/UniswapV3Pool.json',
  SwapRouter:'@uniswap/v3-periphery/artifacts/contracts/SwapRouter.sol/SwapRouter.json',
  QuoterV2:'@uniswap/v3-periphery/artifacts/contracts/lens/QuoterV2.sol/QuoterV2.json',
  NonfungiblePositionManager:'@uniswap/v3-periphery/artifacts/contracts/NonfungiblePositionManager.sol/NonfungiblePositionManager.json'
})) {
  const b = fs.readFileSync(path.join(modules,rel)); const a=JSON.parse(b);
  if (Object.keys(a.linkReferences||{}).length || !/^0x[0-9a-f]+$/i.test(a.bytecode)) throw new Error(name+': unresolved library or invalid bytecode');
  artifacts[name] = {abi:a.abi,bytecode:a.bytecode,deployedBytecode:a.deployedBytecode,origin:rel,artifact_sha256:sha(b)};
}
const router = artifacts.SwapRouter.abi.find(x=>x.name==='exactInputSingle');
if (router.inputs[0].components.length !== 8) throw new Error('Expected canonical eight-field SwapRouter');
const bundle = {schema_version:1,source_revision:'@uniswap/v3-core@1.0.1;@uniswap/v3-periphery@1.4.4;solc@0.8.28',provenance,compiler:solc.version(),settings,artifacts};
fs.mkdirSync(path.join(root,'build'),{recursive:true});
fs.writeFileSync(path.join(root,'build/artifacts.json'),JSON.stringify(bundle,null,2)+'\n');
console.log(JSON.stringify({ok:true,artifacts:Object.keys(artifacts),compiler:solc.version(),bundle_sha256:sha(JSON.stringify(bundle))}));
