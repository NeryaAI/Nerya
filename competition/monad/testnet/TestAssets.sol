// SPDX-License-Identifier: MIT
pragma solidity 0.8.28;

/// Test-only fixed-supply standard ERC20, without taxes/rebasing/mint permissions.
contract TestToken {
    string public name;
    string public symbol;
    uint8 public immutable decimals;
    uint256 public immutable totalSupply;
    mapping(address => uint256) public balanceOf;
    mapping(address => mapping(address => uint256)) public allowance;
    event Transfer(address indexed from, address indexed to, uint256 value);
    event Approval(address indexed owner, address indexed spender, uint256 value);
    constructor(string memory n, string memory s, uint8 d, uint256 supply, address owner) {
        require(owner != address(0));
        name = n; symbol = s; decimals = d; totalSupply = supply;
        balanceOf[owner] = supply;
        emit Transfer(address(0), owner, supply);
    }
    function approve(address spender, uint256 value) external returns (bool) {
        allowance[msg.sender][spender] = value;
        emit Approval(msg.sender, spender, value); return true;
    }
    function transfer(address to, uint256 value) external returns (bool) {
        _transfer(msg.sender, to, value); return true;
    }
    function transferFrom(address from, address to, uint256 value) external returns (bool) {
        uint256 approved = allowance[from][msg.sender];
        require(approved >= value, "allowance");
        allowance[from][msg.sender] = approved - value;
        emit Approval(from, msg.sender, approved - value);
        _transfer(from, to, value); return true;
    }
    function _transfer(address from, address to, uint256 value) internal {
        require(to != address(0) && balanceOf[from] >= value, "balance/address");
        balanceOf[from] -= value; balanceOf[to] += value;
        emit Transfer(from, to, value);
    }
}

/// TEST VALUATION ONLY: chosen units, timestamp is observation time, not price freshness.
contract FixedTestPriceFeed {
    uint8 public constant decimals = 8;
    string public constant description = "Nerya TEST VALUATION - NOT A MARKET ORACLE";
    int256 public immutable fixedTestValue;
    constructor(int256 value) { require(value > 0); fixedTestValue = value; }
    function latestRoundData() external view returns (uint80, int256, uint256, uint256, uint80) {
        return (1, fixedTestValue, block.timestamp, block.timestamp, 1);
    }
}

/// Only NFT metadata is simplified. Minting and liquidity use canonical NFPM.
contract TestPositionDescriptor {
    function tokenURI(address, uint256) external pure returns (string memory) {
        return 'data:application/json,{"name":"Nerya Monad Testnet LP","description":"Test-only canonical Uniswap V3 position; no monetary value"}';
    }
}
