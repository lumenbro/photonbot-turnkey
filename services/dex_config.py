# dex_config.py
"""
Configuration module for DEX routers and their associated XLM contract IDs.
This module can be updated to add support for new DEX routers without modifying the core logic.
"""

DEX_ROUTERS = {
    # AQUA Router
    "6033b4250e704e314fb064973d185db922cae0bd272ba5bff19aac570f12ac2f": {
        "xlm_contract_id": "CAS3J7GYLGXMF6TDJBBYYSE3HQ6BBSMLNUQ34T6TZMYMW2EVH34XOWMA"
    },
    # Soroswap Router
    "4c3db3ebd2d6a2ab23de1f622eaabb39501539b4611b68622ec4e47f76c4ba07": {
        "xlm_contract_id": "CAS3J7GYLGXMF6TDJBBYYSE3HQ6BBSMLNUQ34T6TZMYMW2EVH34XOWMA"
    },
}