"""Screen the latest TWIC PGN archive against captured repertoire positions."""

import argparse
from collections import defaultdict
import gzip
import hashlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import xml.etree.ElementTree as ET
import zipfile

import chess.pgn

ROOT = Path(__file__).resolve().parent
TWIC = 'https://theweekinchess.com'

def fetch(url):
    return subprocess.check_output([
        'curl', '--fail', '--silent', '--show-error', '--location',
        '--max-time', '90', '--header', 'Accept: */*', url,
    ])

def rating(game, color):
    value = game.headers.get(color + 'Elo', '')
    return int(value) if value.isdecimal() else None

def move_label(board, move):
    dots = '.' if board.turn else '...'
    return f'{board.fullmove_number}{dots}{board.san(move)}'

def scan(bundle, index, seen):
    matches = []
    count = skipped = 0
    for name in bundle.namelist():
        if not name.lower().endswith('.pgn'):
            continue
        with io.TextIOWrapper(bundle.open(name), encoding='latin-1') as scores:
            while (game := chess.pgn.read_game(scores)) is not None:
                count += 1
                if game.errors or game.headers.get('FEN'):
                    skipped += 1
                    continue
                board = game.board()
                moves = list(game.mainline_moves())
                junctions = {}
                for ply, move in enumerate(moves[:48], 1):
                    label = move_label(board, move) if ply >= 16 else ''
                    board.push(move)
                    if ply < 16:
                        continue
                    key = hashlib.sha256(board.epd().encode()).hexdigest()[:32]
                    for source, ids in index['positions'].get(key, {}).items():
                        current = junctions.setdefault(source, {
                            'group': key, 'first_match': label, 'first_match_ply': ply,
                        })
                        current.update(deepest_match=label, deepest_match_ply=ply, variations=ids)
                if not junctions:
                    continue
                guid = 'urn:sha256:' + hashlib.sha256(
                    ' '.join(move.uci() for move in moves).encode()
                ).hexdigest()
                if guid in seen:
                    continue
                seen.add(guid)
                for source, match in junctions.items():
                    next_ply = match['deepest_match_ply']
                    if next_ply < len(moves):
                        position = game.board()
                        for move in moves[:next_ply]:
                            position.push(move)
                        match['next_played_move'] = move_label(position, moves[next_ply])
                match = {
                    'guid': guid, 'white': game.headers['White'], 'black': game.headers['Black'],
                    'event': game.headers['Event'], 'date': game.headers['Date'],
                    'result': game.headers['Result'], 'eco': game.headers.get('ECO'),
                    'opening': game.headers.get('Opening'),
                    'white_elo': rating(game, 'White'), 'black_elo': rating(game, 'Black'),
                    'junctions': junctions,
                }
                matches.append((match, game))
    if not count:
        raise RuntimeError('The TWIC archive contained no readable PGN records')
    return count, skipped, matches

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--archive', type=Path, help='Use a local TWIC ZIP for verification.')
    parser.add_argument('--issue', type=int, help='TWIC issue number for a local ZIP.')
    args = parser.parse_args()
    if args.archive and not args.issue:
        parser.error('--archive requires --issue')
    issue = args.issue
    if issue is None:
        issues = re.findall(rb'/zips/twic(\d+)g\.zip', fetch(TWIC + '/twic'))
        if not issues:
            raise RuntimeError('No TWIC issue numbers found in the archive listing')
        issue = max(map(int, issues))
    archive_url = f'{TWIC}/zips/twic{issue}g.zip'
    data = args.archive.read_bytes() if args.archive else fetch(archive_url)
    index = json.loads(gzip.decompress((ROOT / 'repertoire-positions.json.gz').read_bytes()))
    seen = {item.text for item in ET.parse(ROOT / 'docs' / 'feed.xml').findall('./channel/item/guid')}
    with zipfile.ZipFile(io.BytesIO(data)) as bundle:
        count, skipped, matches = scan(bundle, index, seen)

    output = ROOT / 'weekly-output'
    output.mkdir(exist_ok=True)
    groups = defaultdict(list)
    for record, _ in matches:
        for source, junction in record['junctions'].items():
            groups[source, junction['group']].append((record, junction))

    report = {
        'issue': issue, 'source_archive': archive_url, 'records_screened': count,
        'records_skipped': skipped, 'positions_tracked': len(index['positions']),
        'sources': index['sources'], 'unpublished_candidates': [record for record, _ in matches],
    }
    (output / f'TWIC_{issue}_Repertoire_Report.json').write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + '\n', encoding='utf-8'
    )
    if matches:
        with (output / f'TWIC_{issue}_Repertoire_Candidates.pgn').open('w', encoding='utf-8') as handle:
            for record, game in matches:
                game.headers['TWICIssue'] = str(issue)
                game.headers['RepertoireGUID'] = record['guid']
                game.headers['RepertoireMatchPly'] = str(max(
                    junction['deepest_match_ply'] for junction in record['junctions'].values()
                ))
                game.headers['RepertoireSources'] = ', '.join(
                    f"{index['sources'][source]['side']} {index['sources'][source]['source']}"
                    for source in record['junctions']
                )
                handle.write(game.accept(chess.pgn.StringExporter(
                    headers=True, variations=False, comments=False
                )) + '\n\n')

    lines = [
        f'## TWIC {issue}',
        f'Screened {count:,} game records against {len(index["positions"]):,} course-derived positions.',
        f'Found {len(matches):,} unpublished candidates across {len(groups):,} source junctions.',
        f'[Source PGN archive]({archive_url})',
        'These are opening matches awaiting review. The download includes all matching scores and source references.',
    ]
    for source, meta in index['sources'].items():
        total = sum(source in record['junctions'] for record, _ in matches)
        lines.append(f'- {meta["side"]} · {meta["source"]}: {total} matches')
    summary = '\n\n'.join(lines) + '\n'
    print(summary)
    if path := os.environ.get('GITHUB_STEP_SUMMARY'):
        with open(path, 'a', encoding='utf-8') as handle:
            handle.write(summary)

if __name__ == '__main__':
    main()
