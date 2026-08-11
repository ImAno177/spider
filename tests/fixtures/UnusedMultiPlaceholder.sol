pragma solidity ^0.4.25;

contract UnusedMultiPlaceholder {
    enum Mode { On }

    modifier twice() {
        _;
        _;
    }

    modifier inMode(Mode) {
        _;
    }

    function untouched() external {}
    function touched() external twice {}
    function enumTouched() external inMode(Mode.On) {}
}
