"""Early discovery: persistent scout watches outside the TradeCase.

A watch observes a young market from its first valid reading, asks ORBIT about
it at fixed checkpoints and checks VECTOR history maturity. It grants no trading
authority: the scout never opens a case, never reaches SENTINEL and never
executes. A PROMOTABLE watch is only a candidate source for a later, separate
full PAPER run, whose COMMANDER, specialists and SENTINEL decide as before.
"""
