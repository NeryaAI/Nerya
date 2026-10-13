"""Read-only actual public candle signal replay; no trade or PnL simulation."""
import argparse
import importlib.util
import json
from pathlib import Path
import sys

from readers import perpl_snapshot


def replay(snapshot):
    root=Path(__file__).parent/'monad_perp_sma'
    sys.path.insert(0,str(root))
    spec=importlib.util.spec_from_file_location('monad_perp_replay',root/'main.py')
    module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
    candles=sorted(snapshot['candles'],key=lambda row:row['time'])
    computed=[module.signal(candles[:index],int(candles[index-1]['time'])+3600000) for index in range(19,len(candles)+1)]
    return {'scope':'Actual public Monad testnet MON candles; signal replay only; no trades/PnL',
        'source':snapshot['source'],'chain_id':snapshot['chain_id'],'observed_at_ms':snapshot['as_of_ms'],
        'bars':len(candles),'crossings':[row for row in computed if row['signal'] in {'long','short'}],
        'latest':computed[-1] if computed else {'signal':'warmup','bars':len(candles)},'candles':candles}


def main():
    parser=argparse.ArgumentParser();parser.add_argument('--output',type=Path,required=True);args=parser.parse_args()
    result=replay(perpl_snapshot())
    args.output.parent.mkdir(parents=True,exist_ok=True)
    args.output.write_text(json.dumps(result,indent=2)+chr(10))
    print(json.dumps({key:result[key] for key in ('scope','bars','latest')},indent=2))


if __name__=='__main__':main()
