// Provenance chaincode: one key per build id, immutable once anchored.
//
// The MACP node computes a fingerprint over the whole build record and submits
// it here.  Several organisations hold a copy, so a single operator cannot
// rewrite a build's history unnoticed.  Re-anchoring the same build id with the
// same fingerprint is idempotent; with a different fingerprint it is rejected.
package main

import (
	"encoding/json"
	"fmt"
	"time"

	"github.com/hyperledger/fabric-chaincode-go/shim"
	"github.com/hyperledger/fabric-protos-go/peer"
)

// Record is what goes on the ledger.
type Record struct {
	BuildID     string `json:"build_id"`
	Fingerprint string `json:"fingerprint"`
	ArtifactSHA string `json:"artifact_sha256,omitempty"`
	Project     string `json:"project,omitempty"`
	Task        string `json:"task,omitempty"`
	Builder     string `json:"builder,omitempty"`
	CreatedAt   string `json:"created_at,omitempty"`
	AnchoredAt  string `json:"anchored_at"`
	TxID        string `json:"tx_id"`
}

type chaincode struct{}

func (c *chaincode) Init(stub shim.ChaincodeStubInterface) peer.Response {
	return shim.Success(nil)
}

// Invoke dispatches anchor, query and history.
func (c *chaincode) Invoke(stub shim.ChaincodeStubInterface) peer.Response {
	function, args := stub.GetFunctionAndParameters()
	switch function {
	case "anchor":
		return c.anchor(stub, args)
	case "query":
		return c.query(stub, args)
	case "history":
		return c.history(stub, args)
	default:
		return shim.Error(fmt.Sprintf("unknown function %q (expected anchor, query, history)", function))
	}
}

// anchor <buildID> <fingerprint> [artifactSHA] [project] [task] [builder] [createdAt]
func (c *chaincode) anchor(stub shim.ChaincodeStubInterface, args []string) peer.Response {
	if len(args) < 2 {
		return shim.Error("anchor expects at least <buildID> <fingerprint>")
	}
	buildID := args[0]
	fingerprint := args[1]
	if len(fingerprint) != 64 {
		return shim.Error("fingerprint must be a sha256 hex string")
	}

	existing, err := stub.GetState(buildID)
	if err != nil {
		return shim.Error("read state: " + err.Error())
	}
	if existing != nil {
		var current Record
		if err := json.Unmarshal(existing, &current); err != nil {
			return shim.Error("decode existing record: " + err.Error())
		}
		if current.Fingerprint != fingerprint {
			return shim.Error("build " + buildID + " is already anchored with a different fingerprint")
		}
		return shim.Success(mustJSON(current))
	}

	record := Record{
		BuildID:     buildID,
		Fingerprint: fingerprint,
		AnchoredAt:  time.Now().UTC().Format(time.RFC3339),
		TxID:        stub.GetTxID(),
	}
	if len(args) > 2 {
		record.ArtifactSHA = args[2]
	}
	if len(args) > 3 {
		record.Project = args[3]
	}
	if len(args) > 4 {
		record.Task = args[4]
	}
	if len(args) > 5 {
		record.Builder = args[5]
	}
	if len(args) > 6 {
		record.CreatedAt = args[6]
	}

	if err := stub.PutState(buildID, mustJSON(record)); err != nil {
		return shim.Error("write state: " + err.Error())
	}
	return shim.Success(mustJSON(record))
}

// query <buildID>
func (c *chaincode) query(stub shim.ChaincodeStubInterface, args []string) peer.Response {
	if len(args) != 1 {
		return shim.Error("query expects <buildID>")
	}
	raw, err := stub.GetState(args[0])
	if err != nil {
		return shim.Error("read state: " + err.Error())
	}
	if raw == nil {
		return shim.Error("not found: " + args[0])
	}
	return shim.Success(raw)
}

// history <buildID> returns every version of the key with its transaction id.
func (c *chaincode) history(stub shim.ChaincodeStubInterface, args []string) peer.Response {
	if len(args) != 1 {
		return shim.Error("history expects <buildID>")
	}
	iter, err := stub.GetHistoryForKey(args[0])
	if err != nil {
		return shim.Error("history: " + err.Error())
	}
	defer iter.Close()

	out := make([]map[string]any, 0)
	for iter.HasNext() {
		item, err := iter.Next()
		if err != nil {
			return shim.Error("history iterate: " + err.Error())
		}
		var value any
		if item.Value != nil {
			_ = json.Unmarshal(item.Value, &value)
		}
		out = append(out, map[string]any{
			"tx_id":     item.TxId,
			"timestamp": item.Timestamp.AsTime().UTC().Format(time.RFC3339),
			"is_delete": item.IsDelete,
			"record":    value,
		})
	}
	return shim.Success(mustJSON(out))
}

func mustJSON(value any) []byte {
	payload, err := json.Marshal(value)
	if err != nil {
		return []byte("{}")
	}
	return payload
}

func main() {
	if err := shim.Start(&chaincode{}); err != nil {
		panic(err)
	}
}
