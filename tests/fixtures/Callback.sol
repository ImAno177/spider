pragma solidity 0.4.25;

contract CallbackTreasury {
    CallbackVault public vault;

    function record() external {
        vault.onERC721Received();
    }
}

contract CallbackVault {
    CallbackTreasury public treasury;
    uint256 public completed;

    function forward() public {
        treasury.record();
    }

    function onERC721Received() public {
        completed += 1;
    }
}
