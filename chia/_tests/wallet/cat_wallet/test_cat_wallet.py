from __future__ import annotations

import asyncio
import tempfile
from pathlib import Path
from typing import Optional

import pytest

from chia._tests.conftest import ConsensusMode
from chia._tests.environments.wallet import WalletEnvironment, WalletStateTransition, WalletTestFramework
from chia._tests.util.setup_nodes import OldSimulatorsAndWallets, SimulatorsAndWalletsServices
from chia._tests.util.time_out_assert import time_out_assert, time_out_assert_not_none
from chia.consensus.block_rewards import calculate_base_farmer_reward, calculate_pool_reward
from chia.protocols.wallet_protocol import CoinState
from chia.rpc.wallet_rpc_client import WalletRpcClient
from chia.simulator.simulator_protocol import FarmNewBlockProtocol, ReorgProtocol
from chia.types.blockchain_format.coin import Coin, coin_as_list
from chia.types.blockchain_format.program import Program
from chia.types.blockchain_format.sized_bytes import bytes32
from chia.types.coin_spend import make_spend
from chia.types.peer_info import PeerInfo
from chia.util.bech32m import encode_puzzle_hash
from chia.util.db_wrapper import DBWrapper2
from chia.util.ints import uint16, uint32, uint64
from chia.wallet.cat_wallet.cat_constants import DEFAULT_CATS
from chia.wallet.cat_wallet.cat_info import LegacyCATInfo
from chia.wallet.cat_wallet.cat_utils import CAT_MOD, construct_cat_puzzle
from chia.wallet.cat_wallet.cat_wallet import CATWallet
from chia.wallet.derivation_record import DerivationRecord
from chia.wallet.derive_keys import master_pk_to_wallet_pk_unhardened
from chia.wallet.lineage_proof import LineageProof
from chia.wallet.puzzles.p2_delegated_puzzle_or_hidden_puzzle import puzzle_hash_for_pk
from chia.wallet.util.tx_config import DEFAULT_COIN_SELECTION_CONFIG, DEFAULT_TX_CONFIG
from chia.wallet.util.wallet_types import WalletType
from chia.wallet.wallet_info import WalletInfo
from chia.wallet.wallet_interested_store import WalletInterestedStore
from chia.wallet.wallet_node import WalletNode
from chia.wallet.wallet_state_manager import WalletStateManager


def check_wallets(node: WalletNode) -> int:
    return len(node.wallet_state_manager.wallets.keys())


@pytest.mark.parametrize("trusted", [True, False])
@pytest.mark.anyio
async def test_cat_creation(self_hostname: str, two_wallet_nodes: OldSimulatorsAndWallets, trusted: bool) -> None:
    num_blocks = 3
    full_nodes, wallets, _ = two_wallet_nodes
    full_node_api = full_nodes[0]
    full_node_server = full_node_api.server
    wallet_node, server_2 = wallets[0]
    wallet = wallet_node.wallet_state_manager.main_wallet

    ph = await wallet.get_new_puzzlehash()
    if trusted:
        wallet_node.config["trusted_peers"] = {full_node_server.node_id.hex(): full_node_server.node_id.hex()}
    else:
        wallet_node.config["trusted_peers"] = {}

    await server_2.start_client(PeerInfo(self_hostname, full_node_server.get_port()), None)
    for _ in range(num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(bytes32(32 * b"0")))

    funds = sum(
        calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks + 1)
    )

    await time_out_assert(20, wallet.get_confirmed_balance, funds)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node, timeout=20)

    async with wallet_node.wallet_state_manager.lock:
        cat_wallet, tx_records = await CATWallet.create_new_cat_wallet(
            wallet_node.wallet_state_manager,
            wallet,
            {"identifier": "genesis_by_id"},
            uint64(100),
            DEFAULT_TX_CONFIG,
            fee=uint64(10),
        )
        # The next 2 lines are basically a noop, it just adds test coverage
        cat_wallet = await CATWallet.create(wallet_node.wallet_state_manager, wallet, cat_wallet.wallet_info)
        await wallet_node.wallet_state_manager.add_new_wallet(cat_wallet)

    await full_node_api.process_transaction_records(records=tx_records)

    await time_out_assert(20, cat_wallet.get_confirmed_balance, 100)
    await time_out_assert(20, cat_wallet.get_spendable_balance, 100)
    await time_out_assert(20, cat_wallet.get_unconfirmed_balance, 100)
    await time_out_assert(20, wallet.get_confirmed_balance, funds - 110)
    await time_out_assert(20, wallet.get_spendable_balance, funds - 110)
    await time_out_assert(20, wallet.get_unconfirmed_balance, funds - 110)

    # Test migration
    all_lineage = await cat_wallet.lineage_store.get_all_lineage_proofs()
    current_info = cat_wallet.wallet_info
    data_str = bytes(
        LegacyCATInfo(
            cat_wallet.cat_info.limitations_program_hash, cat_wallet.cat_info.my_tail, list(all_lineage.items())
        )
    ).hex()
    wallet_info = WalletInfo(current_info.id, current_info.name, current_info.type, data_str)
    new_cat_wallet = await CATWallet.create(wallet_node.wallet_state_manager, wallet, wallet_info)
    assert new_cat_wallet.cat_info.limitations_program_hash == cat_wallet.cat_info.limitations_program_hash
    assert new_cat_wallet.cat_info.my_tail == cat_wallet.cat_info.my_tail
    assert await cat_wallet.lineage_store.get_all_lineage_proofs() == all_lineage

    height = full_node_api.full_node.blockchain.get_peak_height()
    assert height is not None
    await full_node_api.reorg_from_index_to_new_index(
        ReorgProtocol(uint32(height - num_blocks - 1), uint32(height + 1), bytes32(32 * b"1"), None)
    )
    await time_out_assert(20, cat_wallet.get_confirmed_balance, 0)


@pytest.mark.anyio
async def test_cat_creation_unique_lineage_store(self_hostname: str, two_wallet_nodes: OldSimulatorsAndWallets) -> None:
    num_blocks = 3
    full_nodes, wallets, _ = two_wallet_nodes
    full_node_api = full_nodes[0]
    full_node_server = full_node_api.server
    wallet_node, wallet_server = wallets[0]
    wallet = wallet_node.wallet_state_manager.main_wallet
    ph = await wallet.get_new_puzzlehash()
    wallet_node.config["trusted_peers"] = {full_node_server.node_id.hex(): full_node_server.node_id.hex()}

    await wallet_server.start_client(PeerInfo(self_hostname, full_node_server.get_port()), None)
    for _ in range(num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(bytes32(32 * b"0")))

    funds = sum(
        calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks + 1)
    )

    await time_out_assert(20, wallet.get_confirmed_balance, funds)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node, timeout=20)

    async with wallet_node.wallet_state_manager.lock:
        cat_wallet_1, _ = await CATWallet.create_new_cat_wallet(
            wallet_node.wallet_state_manager, wallet, {"identifier": "genesis_by_id"}, uint64(100), DEFAULT_TX_CONFIG
        )
        cat_wallet_2, _ = await CATWallet.create_new_cat_wallet(
            wallet_node.wallet_state_manager, wallet, {"identifier": "genesis_by_id"}, uint64(200), DEFAULT_TX_CONFIG
        )

    proofs_1 = await cat_wallet_1.lineage_store.get_all_lineage_proofs()
    proofs_2 = await cat_wallet_2.lineage_store.get_all_lineage_proofs()
    assert len(proofs_1) == len(proofs_2)
    assert proofs_1 != proofs_2
    assert cat_wallet_1.lineage_store.table_name != cat_wallet_2.lineage_store.table_name


@pytest.mark.limit_consensus_modes(allowed=[ConsensusMode.PLAIN], reason="irrelevant")
@pytest.mark.parametrize(
    "wallet_environments",
    [
        {
            "num_environments": 2,
            "blocks_needed": [1, 1],
        }
    ],
    indirect=True,
)
@pytest.mark.anyio
async def test_cat_spend(wallet_environments: WalletTestFramework) -> None:
    # Setup
    env_1: WalletEnvironment = wallet_environments.environments[0]
    env_2: WalletEnvironment = wallet_environments.environments[1]
    wallet_node = env_1.node
    wallet_node_2 = env_2.node
    api_0 = env_1.rpc_api
    api_1 = env_2.rpc_api
    wallet = env_1.xch_wallet
    wallet2 = env_2.xch_wallet
    full_node_api = wallet_environments.full_node

    env_1.wallet_aliases = {
        "xch": 1,
        "cat": 2,
    }
    env_2.wallet_aliases = {
        "xch": 1,
        "cat": 2,
    }

    async with wallet_node.wallet_state_manager.lock:
        cat_wallet, tx_records = await CATWallet.create_new_cat_wallet(
            wallet_node.wallet_state_manager, wallet, {"identifier": "genesis_by_id"}, uint64(100), DEFAULT_TX_CONFIG
        )

    await wallet_environments.process_pending_states(
        [
            WalletStateTransition(
                pre_block_balance_updates={
                    "xch": {
                        "unconfirmed_wallet_balance": -100,
                        "<=#spendable_balance": -100,
                        "<=#max_send_amount": -100,
                        ">=#pending_change": 1,  # any amount increase
                        "unspent_coin_count": 0,
                        "pending_coin_removal_count": 1,
                    },
                    "cat": {
                        "init": True,
                        "confirmed_wallet_balance": 0,
                        "unconfirmed_wallet_balance": 100,
                        "spendable_balance": 0,
                        "pending_change": 100,  # A little weird but technically correct
                        "max_send_amount": 0,
                        "unspent_coin_count": 0,
                        "pending_coin_removal_count": 1,  # The ephemeral eve spend
                    },
                },
                post_block_balance_updates={
                    "xch": {
                        "confirmed_wallet_balance": -100,
                        ">=#spendable_balance": 1,  # any amount increase
                        ">=#max_send_amount": 1,  # any amount increase
                        "<=#pending_change": -1,  # any amount decrease
                        "unspent_coin_count": 0,
                        "pending_coin_removal_count": -1,
                    },
                    "cat": {
                        "confirmed_wallet_balance": 100,
                        "spendable_balance": 100,
                        "pending_change": -100,
                        "max_send_amount": 100,
                        "unspent_coin_count": 1,
                        "pending_coin_removal_count": -1,
                    },
                },
            ),
            WalletStateTransition(),
        ]
    )

    assert cat_wallet.cat_info.limitations_program_hash is not None
    asset_id = cat_wallet.get_asset_id()

    cat_wallet_2 = await CATWallet.get_or_create_wallet_for_cat(wallet_node_2.wallet_state_manager, wallet2, asset_id)

    assert cat_wallet.cat_info.limitations_program_hash == cat_wallet_2.cat_info.limitations_program_hash

    cat_2_hash = await cat_wallet_2.get_new_inner_hash()
    tx_records = await cat_wallet.generate_signed_transaction(
        [uint64(60)], [cat_2_hash], DEFAULT_TX_CONFIG, fee=uint64(1)
    )
    tx_id = None
    tx_records = await wallet.wallet_state_manager.add_pending_transactions(tx_records)
    for tx_record in tx_records:
        if tx_record.wallet_id is cat_wallet.id():
            tx_id = tx_record.name.hex()
            assert tx_record.to_puzzle_hash == cat_2_hash
    assert tx_id is not None
    memos = await api_0.get_transaction_memo({"transaction_id": tx_id})
    assert len(memos[tx_id]) == 2  # One for tx, one for change
    assert list(memos[tx_id].values())[0][0] == cat_2_hash.hex()

    await wallet_environments.process_pending_states(
        [
            WalletStateTransition(
                pre_block_balance_updates={
                    "xch": {
                        "unconfirmed_wallet_balance": -1,
                        "<=#spendable_balance": -1,
                        "<=#max_send_amount": -1,
                        ">=#pending_change": 1,  # any amount increase
                        "unspent_coin_count": 0,
                        "pending_coin_removal_count": 1,
                    },
                    "cat": {
                        "unconfirmed_wallet_balance": -60,
                        "spendable_balance": -100,
                        "max_send_amount": -100,
                        "pending_change": 40,
                        "unspent_coin_count": 0,
                        "pending_coin_removal_count": 1,
                    },
                },
                post_block_balance_updates={
                    "xch": {
                        "confirmed_wallet_balance": -1,
                        ">=#spendable_balance": 1,  # any amount increase
                        ">=#max_send_amount": 1,  # any amount increase
                        "<=#pending_change": -1,  # any amount decrease
                        "unspent_coin_count": 0,
                        "pending_coin_removal_count": -1,
                    },
                    "cat": {
                        "confirmed_wallet_balance": -60,
                        "spendable_balance": 40,
                        "max_send_amount": 40,
                        "pending_change": -40,
                        "unspent_coin_count": 0,
                        "pending_coin_removal_count": -1,
                    },
                },
            ),
            WalletStateTransition(
                pre_block_balance_updates={
                    "cat": {
                        "init": True,
                        "confirmed_wallet_balance": 0,
                        "unconfirmed_wallet_balance": 0,
                        "spendable_balance": 0,
                        "pending_change": 0,
                        "max_send_amount": 0,
                        "unspent_coin_count": 0,
                        "pending_coin_removal_count": 0,
                    },
                },
                post_block_balance_updates={
                    "cat": {
                        "confirmed_wallet_balance": 60,
                        "unconfirmed_wallet_balance": 60,
                        "pending_coin_removal_count": 0,
                        "spendable_balance": 60,
                        "max_send_amount": 60,
                        "pending_change": 0,
                        "unspent_coin_count": 1,
                        "pending_coin_removal_count": 0,
                    },
                },
            ),
        ]
    )

    coins = await cat_wallet_2.select_coins(uint64(60), DEFAULT_COIN_SELECTION_CONFIG)
    assert len(coins) == 1
    coin = coins.pop()
    tx_id = coin.name().hex()
    memos = await api_1.get_transaction_memo(dict(transaction_id=tx_id))
    assert len(memos[tx_id]) == 2
    assert list(memos[tx_id].values())[0][0] == cat_2_hash.hex()
    cat_hash = await cat_wallet.get_new_inner_hash()
    tx_records = await cat_wallet_2.generate_signed_transaction([uint64(15)], [cat_hash], DEFAULT_TX_CONFIG)
    tx_records = await wallet2.wallet_state_manager.add_pending_transactions(tx_records)

    await wallet_environments.process_pending_states(
        [
            WalletStateTransition(
                pre_block_balance_updates={},
                post_block_balance_updates={
                    "cat": {
                        "confirmed_wallet_balance": 15,
                        "unconfirmed_wallet_balance": 15,
                        "pending_coin_removal_count": 0,
                        "spendable_balance": 15,
                        "max_send_amount": 15,
                        "pending_change": 0,
                        "unspent_coin_count": 1,
                    },
                },
            ),
            WalletStateTransition(
                pre_block_balance_updates={
                    "cat": {
                        "unconfirmed_wallet_balance": -15,
                        "spendable_balance": -60,
                        "pending_change": 45,
                        "max_send_amount": -60,
                        "unspent_coin_count": 0,
                        "pending_coin_removal_count": 1,
                    },
                },
                post_block_balance_updates={
                    "cat": {
                        "confirmed_wallet_balance": -15,
                        "pending_coin_removal_count": -1,
                        "spendable_balance": 45,
                        "max_send_amount": 45,
                        "pending_change": -45,
                        "unspent_coin_count": 0,
                    },
                },
            ),
        ]
    )

    height = full_node_api.full_node.blockchain.get_peak_height()
    assert height is not None
    await full_node_api.reorg_from_index_to_new_index(
        ReorgProtocol(uint32(height - 1), uint32(height + 1), bytes32(32 * b"1"), None)
    )
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node)
    await env_1.change_balances(
        {
            "cat": {
                "confirmed_wallet_balance": -15,
                "unconfirmed_wallet_balance": -15,
                "pending_coin_removal_count": 0,
                "spendable_balance": -15,
                "max_send_amount": -15,
                "pending_change": 0,
                "unspent_coin_count": -1,
            },
        }
    )
    await env_1.check_balances()


@pytest.mark.parametrize("trusted", [True, False])
@pytest.mark.anyio
async def test_cat_reuse_address(self_hostname: str, two_wallet_nodes: OldSimulatorsAndWallets, trusted: bool) -> None:
    num_blocks = 3
    full_nodes, wallets, _ = two_wallet_nodes
    full_node_api = full_nodes[0]
    full_node_server = full_node_api.server
    wallet_node, server_2 = wallets[0]
    wallet_node_2, server_3 = wallets[1]
    wallet = wallet_node.wallet_state_manager.main_wallet
    wallet2 = wallet_node_2.wallet_state_manager.main_wallet

    ph = await wallet.get_new_puzzlehash()
    if trusted:
        wallet_node.config["trusted_peers"] = {full_node_server.node_id.hex(): full_node_server.node_id.hex()}
        wallet_node_2.config["trusted_peers"] = {full_node_server.node_id.hex(): full_node_server.node_id.hex()}
    else:
        wallet_node.config["trusted_peers"] = {}
        wallet_node_2.config["trusted_peers"] = {}
    await server_2.start_client(PeerInfo(self_hostname, full_node_server.get_port()), None)
    await server_3.start_client(PeerInfo(self_hostname, full_node_server.get_port()), None)

    for _ in range(num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(bytes32(32 * b"0")))

    funds = sum(
        calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks + 1)
    )

    await time_out_assert(20, wallet.get_confirmed_balance, funds)

    async with wallet_node.wallet_state_manager.lock:
        cat_wallet, tx_records = await CATWallet.create_new_cat_wallet(
            wallet_node.wallet_state_manager, wallet, {"identifier": "genesis_by_id"}, uint64(100), DEFAULT_TX_CONFIG
        )
    await full_node_api.process_transaction_records(records=tx_records)

    await time_out_assert(20, cat_wallet.get_confirmed_balance, 100)
    await time_out_assert(20, cat_wallet.get_unconfirmed_balance, 100)

    assert cat_wallet.cat_info.limitations_program_hash is not None
    asset_id = cat_wallet.get_asset_id()

    cat_wallet_2 = await CATWallet.get_or_create_wallet_for_cat(wallet_node_2.wallet_state_manager, wallet2, asset_id)

    assert cat_wallet.cat_info.limitations_program_hash == cat_wallet_2.cat_info.limitations_program_hash

    cat_2_hash = await cat_wallet_2.get_new_inner_hash()
    tx_records = await cat_wallet.generate_signed_transaction(
        [uint64(60)], [cat_2_hash], DEFAULT_TX_CONFIG.override(reuse_puzhash=True), fee=uint64(1)
    )
    tx_records = await wallet.wallet_state_manager.add_pending_transactions(tx_records)
    for tx_record in tx_records:
        if tx_record.wallet_id is cat_wallet.id():
            assert tx_record.to_puzzle_hash == cat_2_hash
            assert tx_record.spend_bundle is not None
            assert len(tx_record.spend_bundle.coin_spends) == 2
            old_puzhash: Optional[str] = None
            for cs in tx_record.spend_bundle.coin_spends:
                if cs.coin.amount == 100:
                    old_puzhash = cs.coin.puzzle_hash.hex()
            assert old_puzhash is not None
            new_puzhash = [c.puzzle_hash.hex() for c in tx_record.additions]
            assert old_puzhash in new_puzhash

    await time_out_assert(15, full_node_api.txs_in_mempool, True, tx_records)

    await time_out_assert(20, cat_wallet.get_pending_change_balance, 40)

    for _ in range(1, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(bytes32(32 * b"\0")))

    await time_out_assert(30, wallet.get_confirmed_balance, funds - 101)

    await time_out_assert(20, cat_wallet.get_confirmed_balance, 40)
    await time_out_assert(20, cat_wallet.get_unconfirmed_balance, 40)

    await time_out_assert(30, cat_wallet_2.get_confirmed_balance, 60)
    await time_out_assert(30, cat_wallet_2.get_unconfirmed_balance, 60)

    cat_hash = await cat_wallet.get_new_inner_hash()
    tx_records = await cat_wallet_2.generate_signed_transaction([uint64(15)], [cat_hash], DEFAULT_TX_CONFIG)
    tx_records = await wallet2.wallet_state_manager.add_pending_transactions(tx_records)

    await time_out_assert(15, full_node_api.txs_in_mempool, True, tx_records)

    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))

    await time_out_assert(20, cat_wallet.get_confirmed_balance, 55)
    await time_out_assert(20, cat_wallet.get_unconfirmed_balance, 55)

    height = full_node_api.full_node.blockchain.get_peak_height()
    assert height is not None
    await full_node_api.reorg_from_index_to_new_index(
        ReorgProtocol(uint32(height - 1), uint32(height + 1), bytes32(32 * b"1"), None)
    )
    await time_out_assert(20, cat_wallet.get_confirmed_balance, 40)


@pytest.mark.parametrize("trusted", [True, False])
@pytest.mark.anyio
async def test_get_wallet_for_asset_id(
    self_hostname: str, two_wallet_nodes: OldSimulatorsAndWallets, trusted: bool
) -> None:
    num_blocks = 3
    full_nodes, wallets, _ = two_wallet_nodes
    full_node_api = full_nodes[0]
    full_node_server = full_node_api.server
    wallet_node, server_2 = wallets[0]
    wallet = wallet_node.wallet_state_manager.main_wallet

    ph = await wallet.get_new_puzzlehash()
    if trusted:
        wallet_node.config["trusted_peers"] = {full_node_server.node_id.hex(): full_node_server.node_id.hex()}
    else:
        wallet_node.config["trusted_peers"] = {}
    await server_2.start_client(PeerInfo(self_hostname, full_node_server.get_port()), None)

    for _ in range(num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(bytes32(32 * b"0")))

    funds = sum(
        calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks + 1)
    )

    await time_out_assert(20, wallet.get_confirmed_balance, funds)

    async with wallet_node.wallet_state_manager.lock:
        cat_wallet, _ = await CATWallet.create_new_cat_wallet(
            wallet_node.wallet_state_manager, wallet, {"identifier": "genesis_by_id"}, uint64(100), DEFAULT_TX_CONFIG
        )

    for _ in range(1, num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(bytes32(32 * b"0")))

    asset_id = cat_wallet.get_asset_id()
    assert cat_wallet.cat_info.my_tail is not None
    await cat_wallet.set_tail_program(bytes(cat_wallet.cat_info.my_tail).hex())
    assert await wallet_node.wallet_state_manager.get_wallet_for_asset_id(asset_id) == cat_wallet

    # Test that the a default CAT will initialize correctly
    asset = DEFAULT_CATS[next(iter(DEFAULT_CATS))]
    asset_id = asset["asset_id"]
    cat_wallet_2 = await CATWallet.get_or_create_wallet_for_cat(wallet_node.wallet_state_manager, wallet, asset_id)
    assert cat_wallet_2.get_name() == asset["name"]
    await cat_wallet_2.set_name("Test Name")
    assert cat_wallet_2.get_name() == "Test Name"


@pytest.mark.parametrize("trusted", [True, False])
@pytest.mark.anyio
async def test_cat_doesnt_see_eve(self_hostname: str, two_wallet_nodes: OldSimulatorsAndWallets, trusted: bool) -> None:
    num_blocks = 3
    full_nodes, wallets, _ = two_wallet_nodes
    full_node_api = full_nodes[0]
    full_node_server = full_node_api.server
    wallet_node, server_2 = wallets[0]
    wallet_node_2, server_3 = wallets[1]
    wallet = wallet_node.wallet_state_manager.main_wallet
    wallet2 = wallet_node_2.wallet_state_manager.main_wallet

    ph = await wallet.get_new_puzzlehash()
    if trusted:
        wallet_node.config["trusted_peers"] = {full_node_server.node_id.hex(): full_node_server.node_id.hex()}
        wallet_node_2.config["trusted_peers"] = {full_node_server.node_id.hex(): full_node_server.node_id.hex()}
    else:
        wallet_node.config["trusted_peers"] = {}
        wallet_node_2.config["trusted_peers"] = {}
    await server_2.start_client(PeerInfo(self_hostname, full_node_server.get_port()), None)
    await server_3.start_client(PeerInfo(self_hostname, full_node_server.get_port()), None)

    for _ in range(num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(bytes32(32 * b"0")))

    funds = sum(
        calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks + 1)
    )

    await time_out_assert(20, wallet.get_confirmed_balance, funds)

    async with wallet_node.wallet_state_manager.lock:
        cat_wallet, tx_records = await CATWallet.create_new_cat_wallet(
            wallet_node.wallet_state_manager, wallet, {"identifier": "genesis_by_id"}, uint64(100), DEFAULT_TX_CONFIG
        )
    await full_node_api.process_transaction_records(records=tx_records)

    await time_out_assert(20, cat_wallet.get_confirmed_balance, 100)
    await time_out_assert(20, cat_wallet.get_unconfirmed_balance, 100)

    assert cat_wallet.cat_info.limitations_program_hash is not None
    asset_id = cat_wallet.get_asset_id()

    cat_wallet_2 = await CATWallet.get_or_create_wallet_for_cat(wallet_node_2.wallet_state_manager, wallet2, asset_id)

    assert cat_wallet.cat_info.limitations_program_hash == cat_wallet_2.cat_info.limitations_program_hash

    cat_2_hash = await cat_wallet_2.get_new_inner_hash()
    tx_records = await cat_wallet.generate_signed_transaction(
        [uint64(60)], [cat_2_hash], DEFAULT_TX_CONFIG, fee=uint64(1)
    )
    tx_records = await wallet.wallet_state_manager.add_pending_transactions(tx_records)
    await full_node_api.process_transaction_records(records=tx_records)

    await time_out_assert(30, wallet.get_confirmed_balance, funds - 101)
    await time_out_assert(30, wallet.get_unconfirmed_balance, funds - 101)

    await time_out_assert(20, cat_wallet.get_confirmed_balance, 40)
    await time_out_assert(20, cat_wallet.get_unconfirmed_balance, 40)

    await time_out_assert(20, cat_wallet_2.get_confirmed_balance, 60)
    await time_out_assert(20, cat_wallet_2.get_unconfirmed_balance, 60)

    cc2_ph = await cat_wallet_2.get_new_cat_puzzle_hash()
    [tx_record] = await wallet.wallet_state_manager.main_wallet.generate_signed_transaction(
        uint64(10), cc2_ph, DEFAULT_TX_CONFIG
    )
    [tx_record] = await wallet.wallet_state_manager.add_pending_transactions([tx_record])
    await full_node_api.process_transaction_records(records=[tx_record])

    id = cat_wallet_2.id()
    wsm = cat_wallet_2.wallet_state_manager

    async def query_and_assert_transactions(wsm: WalletStateManager, id: uint32) -> int:
        all_txs = await wsm.tx_store.get_all_transactions_for_wallet(id)
        return len(list(filter(lambda tx: tx.amount == 10, all_txs)))

    await time_out_assert(20, query_and_assert_transactions, 0, wsm, id)
    await time_out_assert(20, wsm.get_confirmed_balance_for_wallet, 60, id)
    await time_out_assert(20, cat_wallet_2.get_confirmed_balance, 60)
    await time_out_assert(20, cat_wallet_2.get_unconfirmed_balance, 60)


@pytest.mark.parametrize("trusted", [True, False])
@pytest.mark.anyio
async def test_cat_spend_multiple(
    self_hostname: str, three_wallet_nodes: OldSimulatorsAndWallets, trusted: bool
) -> None:
    num_blocks = 3
    full_nodes, wallets, _ = three_wallet_nodes
    full_node_api = full_nodes[0]
    full_node_server = full_node_api.server
    wallet_node_0, wallet_server_0 = wallets[0]
    wallet_node_1, wallet_server_1 = wallets[1]
    wallet_node_2, wallet_server_2 = wallets[2]
    wallet_0 = wallet_node_0.wallet_state_manager.main_wallet
    wallet_1 = wallet_node_1.wallet_state_manager.main_wallet
    wallet_2 = wallet_node_2.wallet_state_manager.main_wallet

    ph = await wallet_0.get_new_puzzlehash()
    if trusted:
        wallet_node_0.config["trusted_peers"] = {full_node_server.node_id.hex(): full_node_server.node_id.hex()}
        wallet_node_1.config["trusted_peers"] = {full_node_server.node_id.hex(): full_node_server.node_id.hex()}
        wallet_node_2.config["trusted_peers"] = {full_node_server.node_id.hex(): full_node_server.node_id.hex()}
    else:
        wallet_node_0.config["trusted_peers"] = {}
        wallet_node_1.config["trusted_peers"] = {}
        wallet_node_2.config["trusted_peers"] = {}
    await wallet_server_0.start_client(PeerInfo(self_hostname, full_node_server.get_port()), None)
    await wallet_server_1.start_client(PeerInfo(self_hostname, full_node_server.get_port()), None)
    await wallet_server_2.start_client(PeerInfo(self_hostname, full_node_server.get_port()), None)

    for _ in range(num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))

    funds = sum(
        calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks)
    )

    await time_out_assert(20, wallet_0.get_confirmed_balance, funds)

    async with wallet_node_0.wallet_state_manager.lock:
        cat_wallet_0, tx_records = await CATWallet.create_new_cat_wallet(
            wallet_node_0.wallet_state_manager,
            wallet_0,
            {"identifier": "genesis_by_id"},
            uint64(100),
            DEFAULT_TX_CONFIG,
        )
    await full_node_api.process_transaction_records(records=tx_records)

    await time_out_assert(20, cat_wallet_0.get_confirmed_balance, 100)
    await time_out_assert(20, cat_wallet_0.get_unconfirmed_balance, 100)

    assert cat_wallet_0.cat_info.limitations_program_hash is not None
    asset_id = cat_wallet_0.get_asset_id()

    cat_wallet_1 = await CATWallet.get_or_create_wallet_for_cat(wallet_node_1.wallet_state_manager, wallet_1, asset_id)

    cat_wallet_2 = await CATWallet.get_or_create_wallet_for_cat(wallet_node_2.wallet_state_manager, wallet_2, asset_id)

    assert cat_wallet_0.cat_info.limitations_program_hash == cat_wallet_1.cat_info.limitations_program_hash
    assert cat_wallet_0.cat_info.limitations_program_hash == cat_wallet_2.cat_info.limitations_program_hash

    cat_1_hash = await cat_wallet_1.get_new_inner_hash()
    cat_2_hash = await cat_wallet_2.get_new_inner_hash()

    tx_records = await cat_wallet_0.generate_signed_transaction(
        [uint64(60), uint64(20)], [cat_1_hash, cat_2_hash], DEFAULT_TX_CONFIG
    )
    tx_records = await wallet_0.wallet_state_manager.add_pending_transactions(tx_records)
    await full_node_api.process_transaction_records(records=tx_records)

    await time_out_assert(20, cat_wallet_0.get_confirmed_balance, 20)
    await time_out_assert(20, cat_wallet_0.get_unconfirmed_balance, 20)

    await time_out_assert(30, cat_wallet_1.get_confirmed_balance, 60)
    await time_out_assert(30, cat_wallet_1.get_unconfirmed_balance, 60)

    await time_out_assert(30, cat_wallet_2.get_confirmed_balance, 20)
    await time_out_assert(30, cat_wallet_2.get_unconfirmed_balance, 20)

    cat_hash = await cat_wallet_0.get_new_inner_hash()

    tx_records = await cat_wallet_1.generate_signed_transaction([uint64(15)], [cat_hash], DEFAULT_TX_CONFIG)
    tx_records = await wallet_1.wallet_state_manager.add_pending_transactions(tx_records)

    tx_records_2 = await cat_wallet_2.generate_signed_transaction([uint64(20)], [cat_hash], DEFAULT_TX_CONFIG)
    tx_records_2 = await wallet_2.wallet_state_manager.add_pending_transactions(tx_records_2)

    await full_node_api.process_transaction_records(records=[*tx_records, *tx_records_2])

    await time_out_assert(20, cat_wallet_0.get_confirmed_balance, 55)
    await time_out_assert(20, cat_wallet_0.get_unconfirmed_balance, 55)

    await time_out_assert(30, cat_wallet_1.get_confirmed_balance, 45)
    await time_out_assert(30, cat_wallet_1.get_unconfirmed_balance, 45)

    await time_out_assert(30, cat_wallet_2.get_confirmed_balance, 0)
    await time_out_assert(30, cat_wallet_2.get_unconfirmed_balance, 0)

    txs = await wallet_1.wallet_state_manager.tx_store.get_transactions_between(cat_wallet_1.id(), 0, 100000)
    # Test with Memo
    tx_records_3 = await cat_wallet_1.generate_signed_transaction(
        [uint64(30)], [cat_hash], DEFAULT_TX_CONFIG, memos=[[b"Markus Walburg"]]
    )
    with pytest.raises(ValueError):
        await cat_wallet_1.generate_signed_transaction(
            [uint64(30)], [cat_hash], DEFAULT_TX_CONFIG, memos=[[b"too"], [b"many"], [b"memos"]]
        )

    tx_records_3 = await wallet_1.wallet_state_manager.add_pending_transactions(tx_records_3)
    await time_out_assert(15, full_node_api.txs_in_mempool, True, tx_records_3)
    txs = await wallet_1.wallet_state_manager.tx_store.get_transactions_between(cat_wallet_1.id(), 0, 100000)
    for tx in txs:
        if tx.amount == 30:
            memos = tx.get_memos()
            assert len(memos) == 2  # One for tx, one for change
            assert b"Markus Walburg" in [v for v_list in memos.values() for v in v_list]
            assert tx.spend_bundle is not None
            assert list(memos.keys())[0] in [a.name() for a in tx.spend_bundle.additions()]


@pytest.mark.parametrize("trusted", [True, False])
@pytest.mark.anyio
async def test_cat_max_amount_send(
    self_hostname: str, two_wallet_nodes: OldSimulatorsAndWallets, trusted: bool
) -> None:
    num_blocks = 3
    full_nodes, wallets, _ = two_wallet_nodes
    full_node_api = full_nodes[0]
    full_node_server = full_node_api.server
    wallet_node, server_2 = wallets[0]
    wallet_node_2, server_3 = wallets[1]
    wallet = wallet_node.wallet_state_manager.main_wallet

    ph = await wallet.get_new_puzzlehash()
    if trusted:
        wallet_node.config["trusted_peers"] = {full_node_server.node_id.hex(): full_node_server.node_id.hex()}
        wallet_node_2.config["trusted_peers"] = {full_node_server.node_id.hex(): full_node_server.node_id.hex()}
    else:
        wallet_node.config["trusted_peers"] = {}
        wallet_node_2.config["trusted_peers"] = {}
    await server_2.start_client(PeerInfo(self_hostname, full_node_server.get_port()), None)
    await server_3.start_client(PeerInfo(self_hostname, full_node_server.get_port()), None)

    for _ in range(num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(bytes32(32 * b"0")))

    funds = sum(
        calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks + 1)
    )

    await time_out_assert(20, wallet.get_confirmed_balance, funds)

    async with wallet_node.wallet_state_manager.lock:
        cat_wallet, tx_records = await CATWallet.create_new_cat_wallet(
            wallet_node.wallet_state_manager, wallet, {"identifier": "genesis_by_id"}, uint64(100000), DEFAULT_TX_CONFIG
        )
    await full_node_api.process_transaction_records(records=tx_records)

    await time_out_assert(20, cat_wallet.get_confirmed_balance, 100000)
    await time_out_assert(20, cat_wallet.get_unconfirmed_balance, 100000)

    assert cat_wallet.cat_info.limitations_program_hash is not None

    cat_2 = await cat_wallet.get_new_inner_puzzle()
    cat_2_hash = cat_2.get_tree_hash()
    amounts = []
    puzzle_hashes = []
    for i in range(1, 50):
        amounts.append(uint64(i))
        puzzle_hashes.append(cat_2_hash)
    spent_coint = (await cat_wallet.get_cat_spendable_coins())[0].coin
    tx_records = await cat_wallet.generate_signed_transaction(
        amounts, puzzle_hashes, DEFAULT_TX_CONFIG, coins={spent_coint}
    )
    tx_records = await wallet.wallet_state_manager.add_pending_transactions(tx_records)
    await full_node_api.process_transaction_records(records=tx_records)

    await asyncio.sleep(2)

    async def check_all_there() -> bool:
        spendable = await cat_wallet.get_cat_spendable_coins()
        spendable_name_set = set()
        for record in spendable:
            spendable_name_set.add(record.coin.name())
        puzzle_hash = construct_cat_puzzle(CAT_MOD, cat_wallet.cat_info.limitations_program_hash, cat_2).get_tree_hash()
        for i in range(1, 50):
            coin = Coin(spent_coint.name(), puzzle_hash, uint64(i))
            if coin.name() not in spendable_name_set:
                return False
        return True

    await time_out_assert(20, check_all_there, True)
    await asyncio.sleep(5)
    max_sent_amount = await cat_wallet.get_max_send_amount()

    # 1) Generate transaction that is under the limit
    [transaction_record] = await cat_wallet.generate_signed_transaction(
        [uint64(max_sent_amount - 1)], [ph], DEFAULT_TX_CONFIG
    )
    assert transaction_record.amount == uint64(max_sent_amount - 1)

    # 2) Generate transaction that is equal to limit
    [transaction_record] = await cat_wallet.generate_signed_transaction(
        [uint64(max_sent_amount)], [ph], DEFAULT_TX_CONFIG
    )
    assert transaction_record.amount == uint64(max_sent_amount)

    # 3) Generate transaction that is greater than limit
    with pytest.raises(ValueError):
        await cat_wallet.generate_signed_transaction([uint64(max_sent_amount + 1)], [ph], DEFAULT_TX_CONFIG)


@pytest.mark.parametrize("trusted", [True, False])
@pytest.mark.parametrize("autodiscovery", [True, False])
@pytest.mark.anyio
async def test_cat_hint(
    self_hostname: str, two_wallet_nodes: OldSimulatorsAndWallets, trusted: bool, autodiscovery: bool
) -> None:
    num_blocks = 3
    full_nodes, wallets, _ = two_wallet_nodes
    full_node_api = full_nodes[0]
    full_node_server = full_node_api.server
    wallet_node, server_2 = wallets[0]
    wallet_node_2, server_3 = wallets[1]
    wallet = wallet_node.wallet_state_manager.main_wallet
    wallet2 = wallet_node_2.wallet_state_manager.main_wallet

    ph = await wallet.get_new_puzzlehash()
    if trusted:
        wallet_node.config["trusted_peers"] = {full_node_server.node_id.hex(): full_node_server.node_id.hex()}
        wallet_node_2.config["trusted_peers"] = {full_node_server.node_id.hex(): full_node_server.node_id.hex()}
    else:
        wallet_node.config["trusted_peers"] = {}
        wallet_node_2.config["trusted_peers"] = {}
    wallet_node.config["automatically_add_unknown_cats"] = autodiscovery
    wallet_node_2.config["automatically_add_unknown_cats"] = autodiscovery
    await server_2.start_client(PeerInfo(self_hostname, full_node_server.get_port()), None)
    await server_3.start_client(PeerInfo(self_hostname, full_node_server.get_port()), None)

    for _ in range(num_blocks):
        await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(ph))
    await full_node_api.farm_new_transaction_block(FarmNewBlockProtocol(bytes32(32 * b"0")))

    funds = sum(
        calculate_pool_reward(uint32(i)) + calculate_base_farmer_reward(uint32(i)) for i in range(1, num_blocks + 1)
    )

    await time_out_assert(20, wallet.get_confirmed_balance, funds)

    async with wallet_node.wallet_state_manager.lock:
        cat_wallet, tx_records = await CATWallet.create_new_cat_wallet(
            wallet_node.wallet_state_manager, wallet, {"identifier": "genesis_by_id"}, uint64(100), DEFAULT_TX_CONFIG
        )
    await full_node_api.process_transaction_records(records=tx_records)

    await time_out_assert(20, cat_wallet.get_confirmed_balance, 100)
    await time_out_assert(20, cat_wallet.get_unconfirmed_balance, 100)
    assert cat_wallet.cat_info.limitations_program_hash is not None

    cat_2_hash = await wallet2.get_new_puzzlehash()
    tx_records = await cat_wallet.generate_signed_transaction(
        [uint64(60)], [cat_2_hash], DEFAULT_TX_CONFIG, memos=[[cat_2_hash]]
    )

    tx_records = await wallet.wallet_state_manager.add_pending_transactions(tx_records)

    await full_node_api.process_transaction_records(records=tx_records)

    await time_out_assert(20, cat_wallet.get_confirmed_balance, 40)
    await time_out_assert(20, cat_wallet.get_unconfirmed_balance, 40)

    if autodiscovery:
        # Autodiscovery enabled: test that wallet was created at this point
        await time_out_assert(20, check_wallets, 2, wallet_node_2)
    else:
        # Autodiscovery disabled: test that no wallet was created
        await time_out_assert(20, check_wallets, 1, wallet_node_2)

    # Then we update the wallet's default CATs
    wallet_node_2.wallet_state_manager.default_cats = {
        cat_wallet.cat_info.limitations_program_hash.hex(): {
            "asset_id": cat_wallet.cat_info.limitations_program_hash.hex(),
            "name": "Test",
            "symbol": "TST",
        }
    }

    # Then we send another transaction
    tx_records = await cat_wallet.generate_signed_transaction(
        [uint64(10)], [cat_2_hash], DEFAULT_TX_CONFIG, memos=[[cat_2_hash]]
    )

    tx_records = await wallet.wallet_state_manager.add_pending_transactions(tx_records)

    await full_node_api.process_transaction_records(records=tx_records)

    await time_out_assert(20, cat_wallet.get_confirmed_balance, 30)
    await time_out_assert(20, cat_wallet.get_unconfirmed_balance, 30)

    # Now we check that another wallet WAS created, even if autodiscovery was disabled
    await time_out_assert(20, check_wallets, 2, wallet_node_2)
    cat_wallet_2 = wallet_node_2.wallet_state_manager.wallets[uint32(2)]
    assert isinstance(cat_wallet_2, CATWallet)

    # Previous balance + balance that triggered creation in case of disabled autodiscovery
    await time_out_assert(30, cat_wallet_2.get_confirmed_balance, 70)
    await time_out_assert(30, cat_wallet_2.get_unconfirmed_balance, 70)

    cat_hash = await cat_wallet.get_new_inner_hash()
    tx_records = await cat_wallet_2.generate_signed_transaction([uint64(5)], [cat_hash], DEFAULT_TX_CONFIG)
    tx_records = await wallet2.wallet_state_manager.add_pending_transactions(tx_records)

    await full_node_api.process_transaction_records(records=tx_records)

    await time_out_assert(20, cat_wallet.get_confirmed_balance, 35)
    await time_out_assert(20, cat_wallet.get_unconfirmed_balance, 35)


@pytest.mark.parametrize("trusted", [True, False])
@pytest.mark.anyio
async def test_cat_change_detection(
    self_hostname: str, one_wallet_and_one_simulator_services: SimulatorsAndWalletsServices, trusted: bool
) -> None:
    num_blocks = 1
    full_nodes, wallets, bt = one_wallet_and_one_simulator_services
    full_node_api = full_nodes[0]._api
    full_node_server = full_node_api.full_node.server
    wallet_service_0 = wallets[0]
    wallet_node_0 = wallet_service_0._node
    wallet_0 = wallet_node_0.wallet_state_manager.main_wallet

    assert wallet_service_0.rpc_server is not None

    client_0 = await WalletRpcClient.create(
        bt.config["self_hostname"],
        wallet_service_0.rpc_server.listen_port,
        wallet_service_0.root_path,
        wallet_service_0.config,
    )
    wallet_node_0.config["automatically_add_unknown_cats"] = True

    if trusted:
        wallet_node_0.config["trusted_peers"] = {
            full_node_api.full_node.server.node_id.hex(): full_node_api.full_node.server.node_id.hex()
        }
    else:
        wallet_node_0.config["trusted_peers"] = {}

    await wallet_node_0.server.start_client(PeerInfo(self_hostname, uint16(full_node_server.get_port())), None)
    await full_node_api.farm_blocks_to_wallet(count=num_blocks, wallet=wallet_0)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=20)

    # Mint CAT to ourselves, immediately spend it to an unhinted puzzle hash that we have manually added to the DB
    # We should pick up this coin as balance even though it is unhinted because it is "change"
    pubkey_unhardened = master_pk_to_wallet_pk_unhardened(
        wallet_node_0.wallet_state_manager.root_pubkey, uint32(100000000)
    )
    inner_puzhash = puzzle_hash_for_pk(pubkey_unhardened)
    puzzlehash_unhardened = construct_cat_puzzle(
        CAT_MOD, Program.to(None).get_tree_hash(), inner_puzhash
    ).get_tree_hash_precalc(inner_puzhash)
    change_derivation = DerivationRecord(
        uint32(0), puzzlehash_unhardened, pubkey_unhardened, WalletType.CAT, uint32(2), False
    )
    # Insert the derivation record before the wallet exists so that it is not subscribed to
    await wallet_node_0.wallet_state_manager.puzzle_store.add_derivation_paths([change_derivation])
    our_puzzle = await wallet_0.get_new_puzzle()
    cat_puzzle = construct_cat_puzzle(
        CAT_MOD,
        Program.to(None).get_tree_hash(),
        Program.to(1),
    )
    addr = encode_puzzle_hash(cat_puzzle.get_tree_hash(), "txch")
    cat_amount_0 = uint64(100)
    cat_amount_1 = uint64(5)

    tx = (await client_0.send_transaction(1, cat_amount_0, addr, DEFAULT_TX_CONFIG)).transaction
    spend_bundle = tx.spend_bundle
    assert spend_bundle is not None

    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, spend_bundle.name())
    await full_node_api.farm_blocks_to_wallet(count=num_blocks, wallet=wallet_0)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=20)

    # Do the eve spend back to our wallet and add the CR layer
    cat_coin = next(c for c in spend_bundle.additions() if c.amount == cat_amount_0)
    next_coin = Coin(
        cat_coin.name(),
        construct_cat_puzzle(CAT_MOD, Program.to(None).get_tree_hash(), our_puzzle).get_tree_hash(),
        cat_amount_0,
    )
    eve_spend, _ = await wallet_node_0.wallet_state_manager.sign_bundle(
        [
            make_spend(
                cat_coin,
                cat_puzzle,
                Program.to(
                    [
                        Program.to(
                            [
                                [51, our_puzzle.get_tree_hash(), cat_amount_0, [our_puzzle.get_tree_hash()]],
                                [51, None, -113, None, None],
                            ]
                        ),
                        None,
                        cat_coin.name(),
                        coin_as_list(cat_coin),
                        [cat_coin.parent_coin_info, Program.to(1).get_tree_hash(), cat_coin.amount],
                        0,
                        0,
                    ]
                ),
            ),
            make_spend(
                next_coin,
                construct_cat_puzzle(CAT_MOD, Program.to(None).get_tree_hash(), our_puzzle),
                Program.to(
                    [
                        [
                            None,
                            (
                                1,
                                [
                                    [51, inner_puzhash, cat_amount_1],
                                    [51, bytes32([0] * 32), cat_amount_0 - cat_amount_1],
                                ],
                            ),
                            None,
                        ],
                        LineageProof(
                            cat_coin.parent_coin_info, Program.to(1).get_tree_hash(), cat_amount_0
                        ).to_program(),
                        next_coin.name(),
                        coin_as_list(next_coin),
                        [next_coin.parent_coin_info, our_puzzle.get_tree_hash(), next_coin.amount],
                        0,
                        0,
                    ]
                ),
            ),
        ],
    )
    await client_0.push_tx(eve_spend)
    await time_out_assert_not_none(5, full_node_api.full_node.mempool_manager.get_spendbundle, eve_spend.name())
    await full_node_api.farm_blocks_to_wallet(count=num_blocks, wallet=wallet_0)
    await full_node_api.wait_for_wallet_synced(wallet_node=wallet_node_0, timeout=20)

    await time_out_assert(20, check_wallets, 2, wallet_node_0)
    cat_wallet = wallet_node_0.wallet_state_manager.wallets[uint32(2)]
    await time_out_assert(20, cat_wallet.get_confirmed_balance, cat_amount_1)
    assert not full_node_api.full_node.subscriptions.has_puzzle_subscription(puzzlehash_unhardened)


@pytest.mark.anyio
async def test_unacknowledged_cat_table() -> None:
    with tempfile.TemporaryDirectory() as temporary_directory:
        db_name = Path(temporary_directory).joinpath("test.sqlite")
        db_name.parent.mkdir(parents=True, exist_ok=True)
        async with DBWrapper2.managed(database=db_name) as db_wrapper:
            interested_store = await WalletInterestedStore.create(db_wrapper)

            def asset_id(i: int) -> bytes32:
                return bytes32([i] * 32)

            def coin_state(i: int) -> CoinState:
                return CoinState(Coin(bytes32([0] * 32), bytes32([0] * 32), uint64(i)), None, None)

            await interested_store.add_unacknowledged_coin_state(asset_id(0), coin_state(0), None)
            await interested_store.add_unacknowledged_coin_state(asset_id(1), coin_state(1), 100)
            assert await interested_store.get_unacknowledged_states_for_asset_id(asset_id(0)) == [(coin_state(0), 0)]
            await interested_store.add_unacknowledged_coin_state(asset_id(0), coin_state(0), None)
            assert await interested_store.get_unacknowledged_states_for_asset_id(asset_id(0)) == [(coin_state(0), 0)]
            assert await interested_store.get_unacknowledged_states_for_asset_id(asset_id(1)) == [(coin_state(1), 100)]
            assert await interested_store.get_unacknowledged_states_for_asset_id(asset_id(2)) == []
            await interested_store.rollback_to_block(50)
            assert await interested_store.get_unacknowledged_states_for_asset_id(asset_id(1)) == []
            await interested_store.delete_unacknowledged_states_for_asset_id(asset_id(1))
            assert await interested_store.get_unacknowledged_states_for_asset_id(asset_id(0)) == [(coin_state(0), 0)]
            await interested_store.delete_unacknowledged_states_for_asset_id(asset_id(0))
            assert await interested_store.get_unacknowledged_states_for_asset_id(asset_id(0)) == []


@pytest.mark.parametrize(
    "wallet_environments",
    [
        {
            "num_environments": 1,
            "blocks_needed": [1],
            "reuse_puzhash": True,  # Parameter doesn't matter for this test
            "config_overrides": {"automatically_add_unknown_cats": True},
        }
    ],
    indirect=True,
)
@pytest.mark.limit_consensus_modes([ConsensusMode.PLAIN], reason="irrelevant")
@pytest.mark.anyio
async def test_cat_melt_balance(wallet_environments: WalletTestFramework) -> None:
    # We push spend bundles direct to full node in this test because
    # we are testing correct observance independent of local state
    env = wallet_environments.environments[0]
    wallet = env.xch_wallet
    simulator = wallet_environments.full_node
    wallet_ph = await wallet.get_puzzle_hash(new=False)

    env.wallet_aliases = {
        "xch": 1,
        "cat": 2,
    }

    ACS = Program.to(1)
    ACS_TAIL = Program.to([])
    ACS_TAIL_HASH = ACS_TAIL.get_tree_hash()
    CAT_w_ACS = construct_cat_puzzle(CAT_MOD, ACS_TAIL_HASH, ACS)
    CAT_w_ACS_HASH = CAT_w_ACS.get_tree_hash()

    from chia.simulator.simulator_protocol import GetAllCoinsProtocol
    from chia.wallet.cat_wallet.cat_utils import SpendableCAT, unsigned_spend_bundle_for_spendable_cats
    from chia.wallet.conditions import UnknownCondition
    from chia.wallet.payment import Payment

    await simulator.farm_blocks_to_puzzlehash(count=1, farm_to=CAT_w_ACS_HASH, guarantee_transaction_blocks=True)
    await simulator.farm_blocks_to_puzzlehash(count=1)
    cat_coin = next(
        c.coin
        for c in await simulator.get_all_coins(GetAllCoinsProtocol(include_spent_coins=False))
        if c.coin.puzzle_hash == CAT_w_ACS_HASH
    )

    tx_amount = 10

    spend_to_wallet = unsigned_spend_bundle_for_spendable_cats(
        CAT_MOD,
        [
            SpendableCAT(
                coin=cat_coin,
                limitations_program_hash=ACS_TAIL_HASH,
                inner_puzzle=ACS,
                inner_solution=Program.to([[51, wallet_ph, tx_amount, [wallet_ph]], [51, None, -113, ACS_TAIL, None]]),
                extra_delta=tx_amount - cat_coin.amount,
            )
        ],
    )
    await env.rpc_client.push_tx(spend_to_wallet)
    await time_out_assert(10, simulator.tx_id_in_mempool, True, spend_to_wallet.name())

    await wallet_environments.process_pending_states(
        [
            WalletStateTransition(
                pre_block_balance_updates={},
                post_block_balance_updates={
                    "xch": {},
                    "cat": {
                        "init": True,
                        "confirmed_wallet_balance": tx_amount,
                        "unconfirmed_wallet_balance": tx_amount,
                        "spendable_balance": tx_amount,
                        "max_send_amount": tx_amount,
                        "unspent_coin_count": 1,
                    },
                },
            )
        ]
    )

    cat_wallet = env.wallet_state_manager.wallets[uint32(2)]
    assert isinstance(cat_wallet, CATWallet)

    # Let's test that continuing to melt this CAT results in the correct balance changes
    for _ in range(0, 5):
        tx_amount -= 1
        new_coin = (await cat_wallet.get_cat_spendable_coins())[0].coin
        new_spend = unsigned_spend_bundle_for_spendable_cats(
            CAT_MOD,
            [
                SpendableCAT(
                    coin=new_coin,
                    limitations_program_hash=ACS_TAIL_HASH,
                    inner_puzzle=await cat_wallet.inner_puzzle_for_cat_puzhash(new_coin.puzzle_hash),
                    inner_solution=wallet.make_solution(
                        primaries=[Payment(wallet_ph, uint64(tx_amount), [wallet_ph])],
                        conditions=(
                            UnknownCondition(
                                opcode=Program.to(51),
                                args=[Program.to(None), Program.to(-113), Program.to(ACS_TAIL), Program.to(None)],
                            ),
                        ),
                    ),
                    extra_delta=-1,
                )
            ],
        )
        signed_spend, _ = await env.wallet_state_manager.sign_bundle(new_spend.coin_spends)
        await env.rpc_client.push_tx(signed_spend)
        await time_out_assert(10, simulator.tx_id_in_mempool, True, signed_spend.name())

        await wallet_environments.process_pending_states(
            [
                WalletStateTransition(
                    pre_block_balance_updates={},
                    post_block_balance_updates={
                        "xch": {},
                        "cat": {
                            "confirmed_wallet_balance": -1,
                            "unconfirmed_wallet_balance": -1,
                            "spendable_balance": -1,
                            "max_send_amount": -1,
                        },
                    },
                )
            ]
        )
