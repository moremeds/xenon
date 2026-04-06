#!/usr/bin/env python3
"""
Options Open Interest Verification

Verifies options flow claims by checking actual Open Interest from UW.
Use this to validate external signals (screenshots, tweets, etc.) before trading.

Usage:
    python3 scripts/verify_options_oi.py MSFT --expiry 2027-01-15
    python3 scripts/verify_options_oi.py MSFT --expiry 2027-01-15 --strikes 575,625,675
    python3 scripts/verify_options_oi.py MSFT --expiry 2027-01-15 --min-strike 500
    python3 scripts/verify_options_oi.py MSFT --expiry 2027-01-15 --json

See docs/options-flow-verification.md for full methodology.
"""

import argparse
import json
import re
import sys
from pathlib import Path
from typing import Optional, List

# Add scripts directory to path
sys.path.insert(0, str(Path(__file__).parent))

from clients.uw_client import UWClient


def parse_strike_from_symbol(symbol: str) -> float:
    """
    Extract strike price from OCC option symbol.
    Format: MSFT270115C00575000 -> strike = 575.00
    Last 8 digits are strike * 1000
    """
    match = re.search(r'[CP](\d{8})$', symbol)
    if match:
        return int(match.group(1)) / 1000
    return 0.0


def parse_option_type_from_symbol(symbol: str) -> str:
    """Extract option type (call/put) from OCC symbol."""
    if 'C' in symbol[-9:-8]:
        return 'call'
    elif 'P' in symbol[-9:-8]:
        return 'put'
    return 'unknown'


def fetch_option_contracts(
    ticker: str,
    expiry: str,
    option_type: Optional[str] = None,
) -> list:
    """Fetch option contracts from UW API."""
    with UWClient() as client:
        contracts = client.get_option_contracts(
            ticker=ticker.upper(),
            expiry=expiry,
            option_type=option_type,
        )
        return contracts.get('data', [])


def analyze_contracts(
    contracts: list,
    min_strike: Optional[float] = None,
    max_strike: Optional[float] = None,
    target_strikes: Optional[List[float]] = None,
) -> list:
    """
    Analyze contracts and extract key metrics.
    """
    results = []
    
    for c in contracts:
        symbol = c.get('option_symbol', '')
        strike = parse_strike_from_symbol(symbol)
        
        if strike == 0:
            continue
            
        # Apply filters
        if min_strike and strike < min_strike:
            continue
        if max_strike and strike > max_strike:
            continue
        if target_strikes and strike not in target_strikes:
            continue
        
        volume = c.get('volume', 0) or 0
        oi = c.get('open_interest', 0) or 0
        prev_oi = c.get('prev_oi', 0) or 0
        total_premium = float(c.get('total_premium', 0) or 0)
        last_price = float(c.get('last_price', 0) or 0)
        iv = float(c.get('implied_volatility', 0) or 0)
        
        # Calculate OI change
        oi_change = oi - prev_oi if prev_oi else None
        
        # Estimate position age (days held based on volume vs OI)
        # If volume << OI, position was opened earlier
        avg_daily_vol = volume if volume > 0 else 1
        position_age_est = oi / avg_daily_vol if avg_daily_vol > 0 else None
        
        results.append({
            'symbol': symbol,
            'strike': strike,
            'option_type': parse_option_type_from_symbol(symbol),
            'volume': volume,
            'open_interest': oi,
            'prev_oi': prev_oi,
            'oi_change': oi_change,
            'total_premium': total_premium,
            'last_price': last_price,
            'implied_volatility': iv,
            'position_age_est_days': position_age_est,
        })
    
    return sorted(results, key=lambda x: x['strike'])


def verify_claim(
    results: list,
    claimed_positions: dict,  # {strike: claimed_contracts}
) -> dict:
    """
    Verify a claimed position against actual OI.
    
    Args:
        results: List of analyzed contracts
        claimed_positions: Dict of {strike: claimed_contracts}
    
    Returns:
        Verification results
    """
    verifications = []
    
    for strike, claimed in claimed_positions.items():
        # Find matching contract
        match = next((r for r in results if r['strike'] == strike), None)
        
        if not match:
            verifications.append({
                'strike': strike,
                'claimed': claimed,
                'actual_oi': 0,
                'verified': False,
                'reason': 'No contract found at this strike',
            })
            continue
        
        actual_oi = match['open_interest']
        
        # Verification logic
        if actual_oi == 0:
            verified = False
            reason = 'OI is zero - no position exists'
        elif abs(actual_oi - abs(claimed)) / abs(claimed) <= 0.10:
            verified = True
            reason = f'OI matches within 10% ({actual_oi:,} vs {abs(claimed):,})'
        elif actual_oi >= abs(claimed):
            verified = True
            reason = f'OI exceeds claim ({actual_oi:,} >= {abs(claimed):,})'
        else:
            verified = False
            reason = f'OI below claim ({actual_oi:,} < {abs(claimed):,})'
        
        verifications.append({
            'strike': strike,
            'claimed': claimed,
            'actual_oi': actual_oi,
            'verified': verified,
            'reason': reason,
            'volume_today': match['volume'],
            'position_held': match['volume'] < actual_oi * 0.5,  # Low volume = held position
        })
    
    all_verified = all(v['verified'] for v in verifications)
    
    return {
        'all_verified': all_verified,
        'verifications': verifications,
    }


def print_results(results: list, ticker: str, expiry: str):
    """Print formatted results."""
    print('=' * 80)
    print(f'OPTIONS OI VERIFICATION: {ticker} {expiry}')
    print('=' * 80)
    print()
    print(f'{"Symbol":<25} {"Strike":>8} {"Type":>6} {"Volume":>10} {"OI":>12} {"OI Chg":>10} {"Premium":>14}')
    print('-' * 95)
    
    for r in results:
        oi_chg_str = f"{r['oi_change']:+,}" if r['oi_change'] is not None else 'N/A'
        print(
            f"{r['symbol']:<25} "
            f"${r['strike']:>7,.0f} "
            f"{r['option_type']:>6} "
            f"{r['volume']:>10,} "
            f"{r['open_interest']:>12,} "
            f"{oi_chg_str:>10} "
            f"${r['total_premium']:>12,.0f}"
        )
    
    print()
    print('=' * 80)
    
    # Summary
    total_oi = sum(r['open_interest'] for r in results)
    total_volume = sum(r['volume'] for r in results)
    total_premium = sum(r['total_premium'] for r in results)
    
    print(f'Total Contracts: {len(results)}')
    print(f'Total OI: {total_oi:,}')
    print(f'Total Volume Today: {total_volume:,}')
    print(f'Total Premium Today: ${total_premium:,.0f}')
    print()


def print_verification(verification: dict):
    """Print verification results."""
    print()
    print('=' * 60)
    print('VERIFICATION RESULTS')
    print('=' * 60)
    print()
    
    for v in verification['verifications']:
        status = '✅ VERIFIED' if v['verified'] else '❌ NOT VERIFIED'
        held = '(HELD)' if v.get('position_held') else '(RECENT)'
        
        print(f"Strike ${v['strike']:,.0f}: {status}")
        print(f"  Claimed: {v['claimed']:,} contracts")
        print(f"  Actual OI: {v['actual_oi']:,} contracts {held}")
        print(f"  Today's Volume: {v.get('volume_today', 'N/A'):,}")
        print(f"  Reason: {v['reason']}")
        print()
    
    overall = '✅ ALL VERIFIED' if verification['all_verified'] else '⚠️ SOME UNVERIFIED'
    print(f'Overall: {overall}')
    print()


def main():
    parser = argparse.ArgumentParser(
        description='Verify options flow claims via Open Interest',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  # Check all Jan 2027 MSFT calls
  python3 scripts/verify_options_oi.py MSFT --expiry 2027-01-15

  # Verify specific strikes from a screenshot
  python3 scripts/verify_options_oi.py MSFT --expiry 2027-01-15 --strikes 575,625,675

  # Verify with claimed sizes
  python3 scripts/verify_options_oi.py MSFT --expiry 2027-01-15 --verify "575:50000,625:100000,675:-50000"
        """
    )
    
    parser.add_argument('ticker', help='Stock ticker symbol')
    parser.add_argument('--expiry', '-e', required=True, help='Expiration date (YYYY-MM-DD)')
    parser.add_argument('--type', '-t', choices=['call', 'put'], help='Option type filter')
    parser.add_argument('--strikes', '-s', help='Comma-separated list of strikes to check')
    parser.add_argument('--min-strike', type=float, help='Minimum strike price')
    parser.add_argument('--max-strike', type=float, help='Maximum strike price')
    parser.add_argument('--verify', '-v', help='Verify claims: "strike1:size1,strike2:size2" (negative = sold)')
    parser.add_argument('--json', action='store_true', help='Output as JSON')
    parser.add_argument('--top', type=int, default=50, help='Show top N contracts by OI (default: 50)')
    
    args = parser.parse_args()
    
    # Parse target strikes
    target_strikes = None
    if args.strikes:
        target_strikes = [float(s.strip()) for s in args.strikes.split(',')]
    
    # Parse verification claims
    claimed_positions = None
    if args.verify:
        claimed_positions = {}
        for pair in args.verify.split(','):
            strike, size = pair.split(':')
            claimed_positions[float(strike.strip())] = int(size.strip())
    
    # Fetch contracts
    try:
        contracts = fetch_option_contracts(args.ticker, args.expiry, args.type)
    except Exception as e:
        print(f'Error fetching contracts: {e}', file=sys.stderr)
        sys.exit(1)
    
    if not contracts:
        print(f'No contracts found for {args.ticker} {args.expiry}', file=sys.stderr)
        sys.exit(1)
    
    # Analyze
    results = analyze_contracts(
        contracts,
        min_strike=args.min_strike,
        max_strike=args.max_strike,
        target_strikes=target_strikes,
    )
    
    # Sort by OI and limit
    results = sorted(results, key=lambda x: x['open_interest'], reverse=True)[:args.top]
    results = sorted(results, key=lambda x: x['strike'])  # Re-sort by strike for display
    
    # Output
    if args.json:
        output = {
            'ticker': args.ticker,
            'expiry': args.expiry,
            'contracts': results,
        }
        if claimed_positions:
            verification = verify_claim(results, claimed_positions)
            output['verification'] = verification
        print(json.dumps(output, indent=2))
    else:
        print_results(results, args.ticker, args.expiry)
        
        if claimed_positions:
            verification = verify_claim(results, claimed_positions)
            print_verification(verification)


if __name__ == '__main__':
    main()
