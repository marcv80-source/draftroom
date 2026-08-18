# lam preview: 0.0 vs 0.10

Generated 2026-08-18T14:50:08.927143+00:00 from the cached real board (`draftroom.validate.board.build_real_board()`). This is a PREVIEW ONLY -- the `lam` default in `valuation/evob.py` stays 0.0 (Marc's explicit preference) until he says otherwise; nothing here changes it.

`dv = evob - lam * sigma_season`. At lam=0.0 the board is risk-neutral (dv == evob). At lam=0.10, a player's dv is penalised by 0.10 points per point of season-total sigma -- so this only actually moves anyone whose `sigma_season` is nonzero (see `DraftValue.sigma_source`: cross-source disagreement is now one way that gets populated instead of reading 'absent').

Top 30 at each lam, merged (sorted by rank at lam=0.10). `move` is the rank change from lam=0.0 to lam=0.10 (positive = moved UP/better, negative = moved DOWN/worse). `NEW`/`OUT` mark players who entered or fell out of the top 30 entirely.

| Rank@0.10 | Rank@0.0 | Move | Player | Pos | dv@0.0 | dv@0.10 | sigma_season |
|---:|---:|:---:|:---|:---:|---:|---:|---:|
| 1 | 1 | - | Jahmyr Gibbs | RB | 143.5 | 142.0 | 15.39 |
| 2 | 2 | - | Bijan Robinson | RB | 139.9 | 138.5 | 14.51 |
| 3 | 3 | - | Puka Nacua | WR | 108.8 | 107.4 | 13.95 |
| 4 | 4 | - | Ja'Marr Chase | WR | 106.5 | 105.6 | 8.81 |
| 5 | 5 | - | Jonathan Taylor | RB | 103.2 | 101.6 | 16.01 |
| 6 | 6 | - | Christian McCaffrey | RB | 98.8 | 96.8 | 19.66 |
| 7 | 7 | - | James Cook III | RB | 94.7 | 94.0 | 6.27 |
| 8 | 8 | - | Josh Allen | QB | 89.2 | 88.5 | 6.92 |
| 9 | 9 | - | Derrick Henry | RB | 88.1 | 87.0 | 11.53 |
| 10 | 10 | - | Jaxon Smith-Njigba | WR | 86.2 | 84.7 | 14.95 |
| 11 | 11 | - | Ashton Jeanty | RB | 81.7 | 81.1 | 5.88 |
| 12 | 13 | UP 1 | Saquon Barkley | RB | 75.5 | 74.5 | 9.30 |
| 13 | 12 | DOWN 1 | Amon-Ra St. Brown | WR | 76.1 | 74.5 | 15.88 |
| 14 | 14 | - | Kenneth Walker | RB | 72.9 | 72.0 | 9.45 |
| 15 | 15 | - | CeeDee Lamb | WR | 71.4 | 70.5 | 8.27 |
| 16 | 16 | - | Chase Brown | RB | 70.8 | 70.0 | 7.99 |
| 17 | 17 | - | Brock Bowers | TE | 67.0 | 66.5 | 4.30 |
| 18 | 18 | - | Omarion Hampton | RB | 63.4 | 62.8 | 6.56 |
| 19 | 19 | - | De'Von Achane | RB | 62.5 | 60.6 | 18.88 |
| 20 | 20 | - | Nico Collins | WR | 60.0 | 59.9 | 1.76 |
| 21 | 21 | - | A.J. Brown | WR | 57.4 | 57.2 | 2.13 |
| 22 | 23 | UP 1 | Lamar Jackson | QB | 56.1 | 55.8 | 3.05 |
| 23 | 22 | DOWN 1 | Jeremiyah Love | RB | 56.9 | 55.6 | 13.05 |
| 24 | 24 | - | George Pickens | WR | 55.5 | 55.0 | 4.71 |
| 25 | 25 | - | Justin Jefferson | WR | 55.2 | 53.9 | 12.70 |
| 26 | 26 | - | Drake London | WR | 55.0 | 53.8 | 11.99 |
| 27 | 27 | - | Trey McBride | TE | 52.2 | 51.7 | 5.63 |
| 28 | 28 | - | Drake Maye | QB | 49.8 | 49.4 | 3.70 |
| 29 | 29 | - | Jalen Hurts | QB | 41.2 | 40.6 | 6.81 |
| 30 | (31) | NEW | Colston Loveland | TE | 39.5 | 39.2 | 3.13 |
| (31) | 30 | OUT | Breece Hall | RB | 41.1 | 39.1 | 20.24 |

Summary: 31 players in the merged top-30 view; 4 moved rank within it, 1 entered under lam=0.10 who weren't in the lam=0.0 top 30, 1 fell out. 