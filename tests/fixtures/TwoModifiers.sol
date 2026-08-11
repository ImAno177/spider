// SPDX-License-Identifier: MIT
pragma solidity ^0.4.25;

contract TwoModifiers {
    bool private locked;
    address private owner;

    modifier onlyOwner() { require(msg.sender == owner); _; }
    modifier nonreentrant() { require(!locked); locked = true; _; locked = false; }

    function act() public onlyOwner nonreentrant { owner = msg.sender; }
}
