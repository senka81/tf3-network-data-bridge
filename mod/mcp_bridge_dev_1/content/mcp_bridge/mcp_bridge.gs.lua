-- MCP Bridge game script. Its resource name (mcp_bridge_dev_1::/mcp_bridge/mcp_bridge.gs) identifies it in saves:
-- keep this resource path stable within the DEV mod. GUI side only; it keeps no saved state.
function data()
	return {
		guiUpdateScript = { fileName = "mcp_bridge.script@guiUpdate" },
	}
end
